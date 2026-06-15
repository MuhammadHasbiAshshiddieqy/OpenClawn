"""Tests untuk Inovasi 2: Skill Decay (exponential decay + revive)."""

import pytest
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.skill_decay import SkillDecayManager


@pytest.fixture
async def db():
    """In-memory DB dengan schema lengkap."""
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


@pytest.fixture
def config():
    return AppConfig(db_path=":memory:", decay_interval_sec=0)


async def _insert_skill(db: DatabaseManager, role: str, name: str, decay_score: float = 1.0) -> int:
    cursor = await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, decay_score, last_used_at)
           VALUES (?,?,?,?, datetime('now', '-10 days'))""",
        (role, name, "isi skill", decay_score),
    )
    return cursor.lastrowid


@pytest.mark.asyncio
async def test_skill_decay_reduces_score(db, config):
    """Inovasi 2: skill tak dipakai harus berkurang decay_score setelah decay pass."""
    decay = SkillDecayManager(role="pm", db=db, config=config)
    await _insert_skill(db, "pm", "skill-lama", decay_score=1.0)

    await decay._run_decay_pass()

    rows = await db.fetchall("SELECT decay_score FROM skills WHERE skill_name='skill-lama'")
    assert rows[0]["decay_score"] < 1.0, "decay_score harus berkurang setelah decay pass"


@pytest.mark.asyncio
async def test_skill_archived_when_below_threshold(db, config):
    """Skill dengan decay_score < 0.3 harus di-arsipkan."""
    decay = SkillDecayManager(role="pm", db=db, config=config)
    await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, decay_score, last_used_at)
           VALUES ('pm', 'skill-tua', 'isi', 0.05, datetime('now', '-100 days'))""",
    )

    result = await decay._run_decay_pass()

    row = await db.fetchone("SELECT status FROM skills WHERE skill_name='skill-tua'")
    assert row["status"] == "archived"
    assert result["archived"] >= 1


@pytest.mark.asyncio
async def test_mark_used_revives_archived_skill(db, config):
    """Skill yang dipakai lagi → status kembali active, score naik."""
    decay = SkillDecayManager(role="pm", db=db, config=config)
    cursor = await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, decay_score, status)
           VALUES ('pm', 'skill-archived', 'isi', 0.1, 'archived')""",
    )
    skill_id = cursor.lastrowid

    await decay.mark_used(skill_id)

    row = await db.fetchone("SELECT status, decay_score FROM skills WHERE id=?", (skill_id,))
    assert row["status"] == "active", "skill archived harus kembali active setelah mark_used"
    assert row["decay_score"] > 0.1, "decay_score harus naik setelah revive"


@pytest.mark.asyncio
async def test_maybe_run_decay_throttled(db):
    """maybe_run_decay_pass harus skip jika belum lewat interval."""
    # interval 9999 detik agar pasti di-throttle
    cfg = AppConfig(db_path=":memory:", decay_interval_sec=9999)
    decay = SkillDecayManager(role="pm", db=db, config=cfg)
    decay._last_decay_ts = 999_999_999_999.0  # timestamp jauh di masa depan

    result = await decay.maybe_run_decay_pass()
    assert result == {"skipped": True}


@pytest.mark.asyncio
async def test_get_active_skills_excludes_archived(db, config):
    """get_active_skills tidak boleh mengembalikan skill archived."""
    decay = SkillDecayManager(role="pm", db=db, config=config)
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('pm','aktif','isi','active')",
    )
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('pm','arsip','isi','archived')",
    )

    skills = await decay.get_active_skills("apapun")
    names = [s["skill_name"] for s in skills]
    assert "aktif" in names
    assert "arsip" not in names
