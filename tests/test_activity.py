"""Test ActivityTimeline: agregasi lintas tabel, filter role, urutan, fail-soft.
DB :memory:, tanpa LLM (hanya baca tabel observability yang sudah ada)."""

import pytest

from core.activity import ActivityTimeline
from infra.config import AppConfig
from infra.database import DatabaseManager


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


async def _seed(db):
    """Isi beberapa peristiwa lintas tabel dengan created_at terurut."""
    await db.execute(
        """INSERT INTO routing_events (session_id, role, query_text, complexity_label,
               model_chosen, provider, had_correction, created_at)
           VALUES ('s1','dev','x','moderate','gemma4:e4b','ollama',0,'2026-06-19 10:00:00')"""
    )
    await db.execute(
        """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms, created_at)
           VALUES ('s1','dev','file_read','ok',12,'2026-06-19 10:01:00')"""
    )
    await db.execute(
        """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms, created_at)
           VALUES ('s1','qa','code_run','error',40,'2026-06-19 10:02:00')"""
    )
    await db.execute(
        """INSERT INTO role_handoffs (session_id, from_role, to_role, task_input,
               contract_name, output_json, validation_ok, created_at)
           VALUES ('s1','pm','dev','buat','dev','{}',1,'2026-06-19 10:03:00')"""
    )
    await db.execute(
        """INSERT INTO crystallization_log (role, skill_name, generator_model, evaluator_model,
               confidence, critical_gaps, status, reasoning, created_at)
           VALUES ('dev','parse_csv','claude-sonnet-4-6','claude-sonnet-4-6',5,0,'active','solid','2026-06-19 10:04:00')"""
    )
    await db.execute(
        """INSERT INTO conversations (session_id, pattern, participants, initial_message,
               transcript_json, turns, end_reason, created_at)
           VALUES ('s1','pipeline','pm,dev,qa','bangun','[]',3,'strategy_done','2026-06-19 10:05:00')"""
    )


async def test_timeline_aggregates_all_sources(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent()
    kinds = {e["kind"] for e in events}
    assert kinds == {"route", "tool", "handoff", "crystallize", "conversation"}
    assert len(events) == 6


async def test_timeline_sorted_newest_first(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent()
    times = [e["created_at"] for e in events]
    assert times == sorted(times, reverse=True)
    # conversation (10:05) terbaru → paling atas.
    assert events[0]["kind"] == "conversation"


async def test_timeline_filter_by_role(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent(role="dev")
    # dev punya: route, tool(file_read), handoff(to_role=dev), crystallize. Bukan qa, bukan conversation.
    for e in events:
        assert e["role"] == "dev"
    kinds = {e["kind"] for e in events}
    assert "conversation" not in kinds  # conversation hanya muncul saat role=None
    assert "route" in kinds and "handoff" in kinds and "crystallize" in kinds


async def test_timeline_outcome_normalized(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent()
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["tool"]["outcome"] in ("ok", "error")  # tool teratas (qa error 10:02)
    assert by_kind["handoff"]["outcome"] == "valid"
    assert by_kind["route"]["outcome"] == "ok"


async def test_timeline_empty_returns_list(db):
    events = await ActivityTimeline(db).recent()
    assert events == []


async def test_timeline_unknown_role_filters_to_empty(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent(role="ghost")
    assert events == []


async def test_timeline_respects_limit(db):
    await _seed(db)
    events = await ActivityTimeline(db).recent(limit=2)
    assert len(events) == 2
    # Tetap terbaru-dulu walau dibatasi.
    assert events[0]["kind"] == "conversation"
