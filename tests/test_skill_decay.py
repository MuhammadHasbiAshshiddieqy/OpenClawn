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


# ── Draft cleanup (gap valid dari review) ─────────────────────────────────────


async def test_stale_unproven_draft_archived(db):
    """Draft TUA & tak terbukti (success_count=0) diarsipkan saat decay pass."""
    cfg = AppConfig(db_path=":memory:", draft_stale_days=14)
    # created_at 30 hari lalu, masih draft, belum pernah terbukti.
    await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, status,
               draft_success_count, created_at)
           VALUES ('dev','tua','isi','draft',0, datetime('now','-30 days'))"""
    )
    decay = SkillDecayManager("dev", db, cfg)
    res = await decay._run_decay_pass()
    assert res["drafts_archived"] == 1
    row = await db.fetchone("SELECT status FROM skills WHERE skill_name='tua'")
    assert row["status"] == "archived"  # diarsipkan, BUKAN dihapus


async def test_recent_draft_not_archived(db):
    """Draft baru tidak diarsipkan (belum basi)."""
    cfg = AppConfig(db_path=":memory:", draft_stale_days=14)
    await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, status,
               draft_success_count, created_at)
           VALUES ('dev','baru','isi','draft',0, datetime('now','-2 days'))"""
    )
    decay = SkillDecayManager("dev", db, cfg)
    res = await decay._run_decay_pass()
    assert res["drafts_archived"] == 0


async def test_proven_draft_not_archived(db):
    """Draft tua TAPI sudah terbukti sebagian (success_count>0) tidak diarsipkan."""
    cfg = AppConfig(db_path=":memory:", draft_stale_days=14)
    await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, status,
               draft_success_count, created_at)
           VALUES ('dev','terbukti','isi','draft',2, datetime('now','-30 days'))"""
    )
    decay = SkillDecayManager("dev", db, cfg)
    res = await decay._run_decay_pass()
    assert res["drafts_archived"] == 0


async def test_draft_cleanup_disabled_when_zero(db):
    """draft_stale_days=0 menonaktifkan cleanup draft."""
    cfg = AppConfig(db_path=":memory:", draft_stale_days=0)
    await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, status,
               draft_success_count, created_at)
           VALUES ('dev','tua','isi','draft',0, datetime('now','-99 days'))"""
    )
    decay = SkillDecayManager("dev", db, cfg)
    res = await decay._run_decay_pass()
    assert res["drafts_archived"] == 0
    row = await db.fetchone("SELECT status FROM skills WHERE skill_name='tua'")
    assert row["status"] == "draft"  # tetap draft


# ── Multi-Tenant isolation (TODO.md § Prioritas 5) ─────────────────────────────


async def test_get_active_skills_scoped_to_tenant(db, config):
    """Skill tenant lain, walau role & trigger sama, tak boleh muncul."""
    await db.execute(
        "INSERT INTO skills (tenant_id, role, skill_name, skill_content, status) "
        "VALUES ('tenant-a','pm','skill-a','isi','active')"
    )
    await db.execute(
        "INSERT INTO skills (tenant_id, role, skill_name, skill_content, status) "
        "VALUES ('tenant-b','pm','skill-b','isi','active')"
    )
    decay_a = SkillDecayManager(role="pm", db=db, config=config, tenant_id="tenant-a")
    decay_b = SkillDecayManager(role="pm", db=db, config=config, tenant_id="tenant-b")

    names_a = [s["skill_name"] for s in await decay_a.get_active_skills("apapun")]
    names_b = [s["skill_name"] for s in await decay_b.get_active_skills("apapun")]
    assert names_a == ["skill-a"]
    assert names_b == ["skill-b"]


async def test_mark_used_cannot_cross_tenant(db, config):
    """Tenant A tak bisa revive/menaikkan skor skill milik tenant B via id tertebak."""
    cursor = await db.execute(
        "INSERT INTO skills (tenant_id, role, skill_name, skill_content, decay_score, status) "
        "VALUES ('tenant-b','pm','skill-victim','isi',0.1,'archived')"
    )
    victim_id = cursor.lastrowid
    decay_a = SkillDecayManager(role="pm", db=db, config=config, tenant_id="tenant-a")

    await decay_a.mark_used(victim_id)

    row = await db.fetchone("SELECT status, decay_score FROM skills WHERE id=?", (victim_id,))
    assert row["status"] == "archived"  # TIDAK ter-revive oleh tenant lain
    assert row["decay_score"] == 0.1


async def test_decay_pass_scoped_to_tenant(db, config):
    """Decay pass tenant A tak menyentuh skill tenant B walau role sama."""
    await db.execute(
        """INSERT INTO skills (tenant_id, role, skill_name, skill_content, decay_score, last_used_at)
           VALUES ('tenant-b','pm','skill-b','isi',1.0, datetime('now','-10 days'))"""
    )
    decay_a = SkillDecayManager(role="pm", db=db, config=config, tenant_id="tenant-a")
    await decay_a._run_decay_pass()

    row = await db.fetchone("SELECT decay_score FROM skills WHERE skill_name='skill-b'")
    assert row["decay_score"] == 1.0  # tak berubah — bukan tenant yang di-decay


async def test_default_tenant_id_backward_compatible(db, config):
    """Tanpa tenant_id eksplisit → 'default', skill lama (schema DEFAULT) tetap terlihat."""
    await _insert_skill(db, "pm", "skill-lama-tanpa-tenant")
    decay = SkillDecayManager(role="pm", db=db, config=config)  # tenant_id default
    skills = await decay.get_active_skills("apapun")
    names = [s["skill_name"] for s in skills]
    assert "skill-lama-tanpa-tenant" in names


# ── Skill Marketplace lintas-role (TODO.md § Prioritas 6) ─────────────────────


async def test_private_skill_not_visible_to_other_role(db, config):
    """Perilaku LAMA tak berubah: skill visibility='private' (default) HANYA
    terlihat oleh role pemiliknya, walau role lain query dengan trigger sama."""
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('pm','rahasia','isi','active','laporan','private')"
    )
    decay_qa = SkillDecayManager(role="qa", db=db, config=config)
    skills = await decay_qa.get_active_skills("buat laporan")
    names = [s["skill_name"] for s in skills]
    assert "rahasia" not in names


async def test_shared_skill_visible_to_other_role(db, config):
    """Skill visibility='shared' dari role LAIN ikut disuntik saat trigger cocok."""
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('pm','template-laporan','isi','active','laporan','shared')"
    )
    decay_qa = SkillDecayManager(role="qa", db=db, config=config)
    skills = await decay_qa.get_active_skills("buat laporan")
    names = [s["skill_name"] for s in skills]
    assert "template-laporan" in names


async def test_inherited_skill_visible_to_other_role(db, config):
    """visibility='inherited' (hasil impor skill pack, core/skill_pack.py) juga
    lintas-role — semantik sama seperti 'shared', beda hanya asal-usulnya."""
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('dev','skill-impor','isi','active','deploy','inherited')"
    )
    decay_qa = SkillDecayManager(role="qa", db=db, config=config)
    skills = await decay_qa.get_active_skills("deploy sekarang")
    names = [s["skill_name"] for s in skills]
    assert "skill-impor" in names


async def test_own_role_skills_not_duplicated_via_shared_query(db, config):
    """Skill milik role sendiri (walau visibility='shared') tak boleh muncul DUA
    KALI — sekali dari query 'active' (role sendiri), bukan lagi dari query 'shared'
    (yang memfilter role!=self)."""
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('pm','sendiri','isi','active','laporan','shared')"
    )
    decay_pm = SkillDecayManager(role="pm", db=db, config=config)
    skills = await decay_pm.get_active_skills("buat laporan")
    names = [s["skill_name"] for s in skills]
    assert names.count("sendiri") == 1


async def test_shared_skills_capped_at_max_shared_skills(db, config):
    """Skill lintas-role dibatasi CONFIG.max_shared_skills — token-first §1.4,
    tak boleh membanjiri context walau banyak skill role lain di-share."""
    cfg = AppConfig(db_path=":memory:", decay_interval_sec=0, max_shared_skills=2)
    for i in range(5):
        await db.execute(
            "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
            f"VALUES ('pm','shared-{i}','isi','active','laporan','shared')"
        )
    decay_qa = SkillDecayManager(role="qa", db=db, config=cfg)
    skills = await decay_qa.get_active_skills("buat laporan")
    shared_names = [s["skill_name"] for s in skills if s["skill_name"].startswith("shared-")]
    assert len(shared_names) == 2


async def test_archived_shared_skill_not_visible(db, config):
    """Skill shared tapi status archived tak boleh muncul lintas-role — sama
    aturan filter status='active' yang berlaku untuk skill role sendiri."""
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('pm','shared-arsip','isi','archived','laporan','shared')"
    )
    decay_qa = SkillDecayManager(role="qa", db=db, config=config)
    skills = await decay_qa.get_active_skills("buat laporan")
    names = [s["skill_name"] for s in skills]
    assert "shared-arsip" not in names


async def test_shared_skill_scoped_to_tenant(db, config):
    """Isolasi tenant (TODO.md § Prioritas 5) tetap berlaku untuk skill shared —
    tenant lain TIDAK bisa lihat skill shared tenant ini walau role beda."""
    await db.execute(
        "INSERT INTO skills (tenant_id, role, skill_name, skill_content, status, trigger_pattern, visibility) "
        "VALUES ('tenant-a','pm','shared-a','isi','active','laporan','shared')"
    )
    decay_tenant_b = SkillDecayManager(role="qa", db=db, config=config, tenant_id="tenant-b")
    skills = await decay_tenant_b.get_active_skills("buat laporan")
    names = [s["skill_name"] for s in skills]
    assert "shared-a" not in names
