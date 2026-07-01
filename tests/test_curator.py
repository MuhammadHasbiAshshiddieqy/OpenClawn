"""Test I1 — Skill Curator: pre-filter, LLM judge gating, merge (anti data-loss), revert."""

import json

from unittest.mock import AsyncMock

import pytest

from core.llm_client import LLMChunk
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.curator import SkillCuratorManager, _jaccard


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


async def _add(db, name, content, trigger="x", role="dev", score=0.5):
    cur = await db.execute(
        """INSERT INTO skills (role, skill_name, skill_content, trigger_pattern, status,
               confidence, generator_model, decay_score, use_count)
           VALUES (?,?,?,?, 'active', 0.6, 'gemma4:e4b', ?, 2)""",
        (role, name, content, trigger, score),
    )
    return cur.lastrowid


def _judge_stream(should_merge, confidence, content="gabungan"):
    async def stream(provider, model, messages, *a, **k):
        yield LLMChunk(
            type="text",
            text=json.dumps(
                {
                    "should_merge": should_merge,
                    "confidence": confidence,
                    "merged_name": "m",
                    "merged_content": content,
                    "reasoning": "mirip",
                }
            ),
        )

    return stream


def _curator(db, llm, **cfg_kw):
    cfg = AppConfig(db_path=":memory:", **cfg_kw)
    return SkillCuratorManager("dev", db, llm, cfg)


# ── similarity pre-filter ──────────────────────────────────────────────────────


def test_jaccard_identical_high():
    assert _jaccard("parse json aman", "parse json aman") == 1.0


def test_jaccard_disjoint_zero():
    assert _jaccard("parse json", "deploy docker") < 0.3


# ── candidate detection ────────────────────────────────────────────────────────


async def test_finds_similar_pair(db):
    await _add(db, "parse_json_a", "cara parse json dengan aman memakai json.loads")
    await _add(db, "parse_json_b", "parse json dengan aman pakai json.loads dan try")
    cur = _curator(db, AsyncMock(), curation_similarity_threshold=0.4)
    pairs = await cur._find_candidate_pairs()
    assert len(pairs) == 1


async def test_no_pair_when_different(db):
    await _add(db, "parse_json", "parse json dengan json.loads")
    await _add(db, "deploy", "deploy aplikasi ke docker container")
    cur = _curator(db, AsyncMock(), curation_similarity_threshold=0.78)
    assert await cur._find_candidate_pairs() == []


# ── merge gating ───────────────────────────────────────────────────────────────


async def test_merge_when_judge_confident(db):
    id_a = await _add(db, "a", "parse json aman dengan json.loads dan try except", score=0.9)
    id_b = await _add(db, "b", "parse json aman pakai json.loads plus try except", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5, "konten gabungan")
    cur = _curator(
        db,
        llm,
        curation_similarity_threshold=0.4,
        curation_judge_min_confidence=4,
        curation_auto=True,
    )
    res = await cur._run_pass()
    assert res["merged"] == 1
    # Winner = score lebih tinggi (id_a) tetap active dgn konten gabungan; loser merged.
    a = await db.fetchone("SELECT status, skill_content, version FROM skills WHERE id=?", (id_a,))
    b = await db.fetchone("SELECT status, merged_into FROM skills WHERE id=?", (id_b,))
    assert a["status"] == "active" and a["skill_content"] == "konten gabungan" and a["version"] == 2
    assert b["status"] == "merged" and b["merged_into"] == id_a


async def test_default_curation_auto_only_proposes(db):
    """curation_auto=False (default, §8): judge confident TAPI skill tak berubah — hanya diusulkan."""
    id_a = await _add(db, "a", "parse json aman dengan json.loads dan try except", score=0.9)
    id_b = await _add(db, "b", "parse json aman pakai json.loads plus try except", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5, "konten gabungan")
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_judge_min_confidence=4)
    assert cur.config.curation_auto is False
    res = await cur._run_pass()
    assert res["merged"] == 0 and res["proposed"] == 1
    # Kedua skill TETAP active — belum ada perubahan sampai manusia meng-apply.
    a = await db.fetchone("SELECT status FROM skills WHERE id=?", (id_a,))
    b = await db.fetchone("SELECT status FROM skills WHERE id=?", (id_b,))
    assert a["status"] == "active" and b["status"] == "active"
    pending = await db.fetchone(
        "SELECT status, winner_id, merged_content FROM curation_log WHERE action='merge'"
    )
    assert pending["status"] == "pending"
    assert pending["winner_id"] == id_a
    assert pending["merged_content"] == "konten gabungan"


async def test_apply_pending_merge_applies_effect(db):
    id_a = await _add(db, "a", "parse json aman dengan json.loads dan try except", score=0.9)
    id_b = await _add(db, "b", "parse json aman pakai json.loads plus try except", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5, "konten gabungan")
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_judge_min_confidence=4)
    await cur._run_pass()
    pending = await db.fetchone("SELECT id FROM curation_log WHERE status='pending'")

    result = await cur.apply_pending_merge(pending["id"])
    assert result["applied"] is True and result["winner_id"] == id_a and result["loser_id"] == id_b

    a = await db.fetchone("SELECT status, skill_content, version FROM skills WHERE id=?", (id_a,))
    b = await db.fetchone("SELECT status, merged_into FROM skills WHERE id=?", (id_b,))
    assert a["status"] == "active" and a["skill_content"] == "konten gabungan" and a["version"] == 2
    assert b["status"] == "merged" and b["merged_into"] == id_a

    row = await db.fetchone("SELECT status FROM curation_log WHERE id=?", (pending["id"],))
    assert row["status"] == "applied"
    # Sekarang ada merge yang benar-benar diterapkan → revert harus berhasil.
    reverted = await cur.revert_last_merge()
    assert reverted["reverted"] is True


async def test_apply_pending_merge_unknown_id_noop(db):
    cur = _curator(db, AsyncMock())
    result = await cur.apply_pending_merge(999)
    assert result["applied"] is False


async def test_revert_ignores_pending_proposals(db):
    """Usulan pending belum mengubah skill apa pun — revert_last_merge harus no-op."""
    await _add(db, "a", "parse json aman dengan json.loads dan try except", score=0.9)
    await _add(db, "b", "parse json aman pakai json.loads plus try except", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5, "konten gabungan")
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_judge_min_confidence=4)
    await cur._run_pass()
    res = await cur.revert_last_merge()
    assert res["reverted"] is False


async def test_no_merge_when_judge_unsure(db):
    await _add(db, "a", "parse json aman dengan json.loads try except", score=0.9)
    await _add(db, "b", "parse json aman pakai json.loads plus try except", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 3)  # confidence < 4
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_judge_min_confidence=4)
    res = await cur._run_pass()
    assert res["merged"] == 0
    rows = await db.fetchall("SELECT status FROM skills")
    assert all(r["status"] == "active" for r in rows)  # tak ada yang merged


# ── anti data-loss + audit ─────────────────────────────────────────────────────


async def test_merge_preserves_loser_and_logs(db):
    id_a = await _add(db, "a", "parse json aman json.loads try except handle", score=0.9)
    id_b = await _add(db, "b", "parse json aman json.loads try except catch", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5)
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_auto=True)
    await cur._run_pass()
    # Loser tidak dihapus (masih ada barisnya, status merged).
    loser = await db.fetchone("SELECT id FROM skills WHERE id=?", (id_b,))
    assert loser is not None
    # curation_log terisi + skill_versions menyimpan konten winner lama.
    clog = await db.fetchone("SELECT action, winner_id FROM curation_log WHERE action='merge'")
    assert clog["winner_id"] == id_a
    ver = await db.fetchone("SELECT reason FROM skill_versions WHERE skill_id=?", (id_a,))
    assert ver["reason"] == "merge"


# ── revert ──────────────────────────────────────────────────────────────────


async def test_revert_restores_loser(db):
    await _add(db, "a", "parse json aman json.loads try except handle", score=0.9)
    id_b = await _add(db, "b", "parse json aman json.loads try except catch", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5, "gabungan baru")
    cur = _curator(db, llm, curation_similarity_threshold=0.4, curation_auto=True)
    await cur._run_pass()
    assert (await db.fetchone("SELECT status FROM skills WHERE id=?", (id_b,)))[
        "status"
    ] == "merged"

    res = await cur.revert_last_merge()
    assert res["reverted"] is True
    b = await db.fetchone("SELECT status, merged_into FROM skills WHERE id=?", (id_b,))
    assert b["status"] == "active" and b["merged_into"] is None


async def test_revert_noop_when_no_merge(db):
    cur = _curator(db, AsyncMock())
    res = await cur.revert_last_merge()
    assert res["reverted"] is False


# ── throttle ──────────────────────────────────────────────────────────────────


async def test_curation_throttled(db):
    await _add(db, "a", "parse json aman json.loads", score=0.9)
    await _add(db, "b", "parse json aman json.loads", score=0.4)
    llm = AsyncMock()
    llm.stream_with_fallback = _judge_stream(True, 5)
    cur = _curator(db, llm, curation_similarity_threshold=0.4)
    first = await cur.maybe_run_curation_pass()
    assert first["skipped"] is False
    second = await cur.maybe_run_curation_pass()
    assert second["skipped"] is True
