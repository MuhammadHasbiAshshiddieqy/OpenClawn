"""Test compounding I2 (draft promotion) + I3 (refine on correction) + prasyarat
(revive skill terpakai). DB :memory:, LLM di-mock untuk refine."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.crystallizer import ConfidenceCrystallizer
from core.llm_client import LLMChunk
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.skill_decay import SkillDecayManager
from memory.skill_feedback import SkillFeedback


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


def _cfg(**kw):
    return AppConfig(db_path=":memory:", **kw)


async def _add_skill(db, role="dev", name="s", status="active", content="isi", trigger="csv"):
    cur = await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, trigger_pattern, status,
               confidence, generator_model, decay_score)
           VALUES (?,?,?,?,?,0.5,'gemma4:e4b',0.6)""",
        (role, name, content, trigger, status),
    )
    return cur.lastrowid


def _feedback(db, cfg, llm=None):
    decay = SkillDecayManager("dev", db, cfg)
    cryst = ConfidenceCrystallizer("dev", llm or AsyncMock(), db)
    return SkillFeedback("dev", db, decay, cryst, cfg)


# ── Prasyarat: revive skill terpakai ──────────────────────────────────────────


async def test_used_active_skill_revived_on_success(db):
    cfg = _cfg()
    sid = await _add_skill(db, status="active")
    fb = _feedback(db, cfg)
    await fb.record_usage("s1", [sid])
    # Turn berikutnya TIDAK mengoreksi → sukses → revive (use_count naik).
    await fb.resolve_previous("s1", corrected=False)
    row = await db.fetchone("SELECT use_count, last_used_at FROM skills WHERE id=?", (sid,))
    assert row["use_count"] == 1
    assert row["last_used_at"] is not None


# ── I2: draft promotion ────────────────────────────────────────────────────────


async def test_draft_promoted_after_n_successes(db):
    cfg = _cfg(draft_promote_uses=3)
    sid = await _add_skill(db, status="draft")
    fb = _feedback(db, cfg)
    for _ in range(3):
        await fb.record_usage("s1", [sid])
        await fb.resolve_previous("s1", corrected=False)
    row = await db.fetchone("SELECT status, draft_success_count FROM skills WHERE id=?", (sid,))
    assert row["status"] == "active"


async def test_draft_reset_on_correction(db):
    cfg = _cfg(draft_promote_uses=3)
    sid = await _add_skill(db, status="draft")
    fb = _feedback(db, cfg)
    # 2 sukses lalu 1 koreksi → counter reset, tetap draft.
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=False)
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=False)
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=True, correction_trace="salah")
    row = await db.fetchone("SELECT status, draft_success_count FROM skills WHERE id=?", (sid,))
    assert row["status"] == "draft"
    assert row["draft_success_count"] == 0


async def test_active_skill_unaffected_by_draft_logic(db):
    cfg = _cfg()
    sid = await _add_skill(db, status="active")
    decay = SkillDecayManager("dev", db, cfg)
    res = await decay.record_draft_outcome(sid, success=True)
    assert res["action"] == "noop"


# ── I3: refine on correction (gated + versioned) ───────────────────────────────


def _refine_stream(improved, confidence, new_content="konten baru"):
    async def stream(provider, model, messages, *a, **k):
        payload = json.dumps(
            {
                "improved": improved,
                "confidence": confidence,
                "new_content": new_content,
                "reasoning": "x",
            }
        )
        yield LLMChunk(type="text", text=payload)

    return stream


async def test_refine_applies_when_confident(db):
    cfg = _cfg(refine_on_correction=True)
    sid = await _add_skill(db, status="active", content="konten lama")
    llm = AsyncMock()
    llm.stream_with_fallback = _refine_stream(True, 5, "konten baru")
    fb = _feedback(db, cfg, llm)
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=True, correction_trace="ini salah")
    row = await db.fetchone("SELECT skill_content, version FROM skills WHERE id=?", (sid,))
    assert row["skill_content"] == "konten baru"
    assert row["version"] == 2
    # Versi lama tersimpan (revertible).
    ver = await db.fetchone("SELECT skill_content FROM skill_versions WHERE skill_id=?", (sid,))
    assert ver["skill_content"] == "konten lama"


async def test_refine_skipped_when_low_confidence(db):
    cfg = _cfg(refine_on_correction=True)
    sid = await _add_skill(db, status="active", content="konten lama")
    llm = AsyncMock()
    llm.stream_with_fallback = _refine_stream(True, 2)
    fb = _feedback(db, cfg, llm)
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=True, correction_trace="salah")
    row = await db.fetchone("SELECT skill_content, version FROM skills WHERE id=?", (sid,))
    assert row["skill_content"] == "konten lama"  # tak berubah
    assert row["version"] == 1


async def test_refine_disabled_by_config(db):
    cfg = _cfg(refine_on_correction=False)
    sid = await _add_skill(db, status="active", content="konten lama")
    llm = AsyncMock()
    fb = _feedback(db, cfg, llm)
    # LLM tak boleh dipanggil sama sekali bila refine dimatikan.
    with patch.object(llm, "stream_with_fallback") as m:
        await fb.record_usage("s1", [sid])
        await fb.resolve_previous("s1", corrected=True, correction_trace="salah")
        m.assert_not_called()
    row = await db.fetchone("SELECT skill_content FROM skills WHERE id=?", (sid,))
    assert row["skill_content"] == "konten lama"


# ── Robustness ──────────────────────────────────────────────────────────────


async def test_resolve_noop_when_no_pending(db):
    fb = _feedback(db, _cfg())
    res = await fb.resolve_previous("ghost", corrected=False)
    assert res["resolved"] == 0


async def test_pending_marked_resolved_once(db):
    cfg = _cfg()
    sid = await _add_skill(db, status="active")
    fb = _feedback(db, cfg)
    await fb.record_usage("s1", [sid])
    await fb.resolve_previous("s1", corrected=False)
    # Resolusi kedua tak menemukan pending (sudah resolved).
    res2 = await fb.resolve_previous("s1", corrected=False)
    assert res2["resolved"] == 0
