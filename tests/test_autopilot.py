"""Test Autopilots: store CRUD, scheduler due/misfire, dan KEAMANAN gating proposal.

Yang paling penting (CLAUDE.md §1, §17): autopilot TIDAK mengeksekusi tool yang butuh
approval — ia mengantrinya sebagai proposal. Ditest di test_autopilot_queues_proposal_*.
DB :memory:, LLM di-mock (fake stream), Docker tak pernah disentuh.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from core.agent_loop import AgentConfig, AgentLoop
from core.autopilot import AutopilotScheduler, AutopilotStore, _iso, _utcnow
from core.llm_client import LLMChunk
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


# ── Store ─────────────────────────────────────────────────────────────────────


async def test_create_and_list(db):
    store = AutopilotStore(db)
    ap_id = await store.create("Audit harian", "security", "audit deps", 3600)
    rows = await store.list_all()
    assert len(rows) == 1
    assert rows[0]["id"] == ap_id
    assert rows[0]["role"] == "security"
    assert rows[0]["enabled"] == 1
    assert rows[0]["next_run_at"] is not None


async def test_interval_floored_to_minimum(db):
    """Interval di bawah MIN_INTERVAL_SEC dinaikkan (cegah spam/biaya)."""
    store = AutopilotStore(db)
    await store.create("x", "dev", "p", 5)
    row = (await store.list_all())[0]
    assert row["interval_sec"] >= 60


async def test_toggle_and_delete(db):
    store = AutopilotStore(db)
    ap_id = await store.create("x", "dev", "p", 3600)
    await store.set_enabled(ap_id, False)
    assert (await store.get(ap_id))["enabled"] == 0
    await store.delete(ap_id)
    assert await store.get(ap_id) is None


async def test_due_returns_only_past_and_enabled(db):
    store = AutopilotStore(db)
    ap_id = await store.create("x", "dev", "p", 3600)
    # Baru dibuat → next_run di masa depan → belum due.
    assert await store.due() == []
    # Geser next_run_at ke masa lalu → due.
    past = _iso(_utcnow() - timedelta(hours=1))
    await db.execute("UPDATE autopilots SET next_run_at=? WHERE id=?", (past, ap_id))
    due = await store.due()
    assert len(due) == 1
    # Disable → tak due lagi walau next_run di masa lalu.
    await store.set_enabled(ap_id, False)
    assert await store.due() == []


async def test_mark_ran_reschedules_forward(db):
    """mark_ran menjadwalkan ulang dari sekarang (misfire-safe, tak menumpuk)."""
    store = AutopilotStore(db)
    ap_id = await store.create("x", "dev", "p", 3600)
    past = _iso(_utcnow() - timedelta(hours=5))
    await db.execute("UPDATE autopilots SET next_run_at=? WHERE id=?", (past, ap_id))
    await store.mark_ran(ap_id, 3600)
    row = await store.get(ap_id)
    # next_run_at kini di masa depan (sekarang + interval), bukan menumpuk dari masa lalu.
    assert row["next_run_at"] > _iso(_utcnow())
    assert row["last_run_at"] is not None
    assert await store.due() == []


# ── Scheduler ──────────────────────────────────────────────────────────────────


async def test_scheduler_runs_due_via_runner(db):
    store = AutopilotStore(db)
    ap_id = await store.create("x", "dev", "p", 3600)
    await db.execute(
        "UPDATE autopilots SET next_run_at=? WHERE id=?",
        (_iso(_utcnow() - timedelta(hours=1)), ap_id),
    )
    ran: list[dict] = []

    async def runner(ap):
        ran.append(ap)
        return 0

    sched = AutopilotScheduler(store, runner=runner)
    count = await sched.run_due_once()
    assert count == 1
    assert ran[0]["id"] == ap_id
    # Setelah jalan → di-reschedule, tak due lagi pada panggilan kedua.
    assert await sched.run_due_once() == 0


async def test_scheduler_records_run_and_survives_runner_error(db):
    store = AutopilotStore(db)
    ap_id = await store.create("x", "dev", "p", 3600)
    await db.execute(
        "UPDATE autopilots SET next_run_at=? WHERE id=?",
        (_iso(_utcnow() - timedelta(hours=1)), ap_id),
    )

    async def boom(ap):
        raise RuntimeError("runner gagal")

    sched = AutopilotScheduler(store, runner=boom)
    await sched.run_due_once()  # tidak boleh raise — fail-soft
    runs = await store.recent_runs()
    assert runs and runs[0]["status"] == "error"
    assert "gagal" in runs[0]["error"]


# ── KEAMANAN: gating proposal di AgentLoop autopilot mode ──────────────────────


def _fake_stream_calling_tool(tool_name: str):
    """LLM mock: giliran pertama panggil tool, giliran kedua jawab teks (stop)."""
    calls = {"n": 0}

    async def stream(provider, model, messages, tools_schema):
        calls["n"] += 1
        if calls["n"] == 1:
            yield LLMChunk(
                type="tool_call", tool_name=tool_name, tool_input={"path": "x.txt", "content": "y"}
            )
        else:
            yield LLMChunk(type="text", text="selesai")
        yield LLMChunk(type="usage", usage={"input_tokens": 1, "output_tokens": 1})

    return stream


async def test_autopilot_queues_proposal_not_executes(db):
    """Tool butuh-approval (file_write) di autopilot → DIANTRI sebagai proposal, TAK dieksekusi."""
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="autopilot-1", autopilot=True),
        db=db,
    )
    with patch.object(
        agent.llm, "stream_with_fallback", side_effect=_fake_stream_calling_tool("file_write")
    ):
        # ApprovalGate.request TIDAK boleh dipanggil di mode autopilot (akan menggantung).
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=True)) as req:
            async for _ev in agent.run("tulis file"):
                pass
            req.assert_not_called()

    # Proposal tercatat sebagai pending di approval_log.
    row = await db.fetchone(
        "SELECT * FROM approval_log WHERE session_id='autopilot-1' AND decision='proposal:pending'"
    )
    assert row is not None
    assert row["tool_name"] == "file_write"


async def test_interactive_mode_still_requests_approval(db):
    """Tanpa autopilot (sesi biasa) → ApprovalGate.request tetap dipanggil (tak ada regresi)."""
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="s-interactive", autopilot=False),
        db=db,
    )
    with patch.object(
        agent.llm, "stream_with_fallback", side_effect=_fake_stream_calling_tool("file_write")
    ):
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=False)) as req:
            async for _ev in agent.run("tulis file"):
                pass
            req.assert_called_once()
