"""Test untuk tools/task_graph_submit.py (§ Task Graph, Fase 1) — validasi
input SEBELUM DB/executor disentuh, dan integrasi dasar dengan DB nyata.

`TaskGraphExecutor.run` sungguhan dipakai di sini (bukan di-mock) untuk kasus
sukses — tapi `AgentLoop` di baliknya TETAP tak pernah benar-benar dipanggil
untuk kasus VALIDASI GAGAL (itulah yang diuji: tak ada eksekusi sama sekali).
Untuk kasus sukses end-to-end, monkeypatch `_build_agent` (module-level di
tools/task_graph_submit.py) dengan `FakeAgentLoop` — pola sama test_task_executor.py.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from tools.task_graph_submit import TaskGraphSubmitTool


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


async def _count_rows(db, table: str) -> int:
    row = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
    return row["n"]


@pytest.mark.asyncio
async def test_missing_session_id_errors(db):
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {"nodes": [{"node_id": "A", "role": "dev", "prompt": "x"}]}, vault=None, db=db
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_missing_db_errors():
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {"_session_id": "s1", "nodes": [{"node_id": "A", "role": "dev", "prompt": "x"}]},
        vault=None,
        db=None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_empty_nodes_errors_without_touching_db(db):
    tool = TaskGraphSubmitTool()
    result = await tool.execute({"_session_id": "s1", "nodes": []}, vault=None, db=db)
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0


@pytest.mark.asyncio
async def test_too_many_nodes_rejected(db):
    tool = TaskGraphSubmitTool()
    nodes = [{"node_id": f"n{i}", "role": "dev", "prompt": "x"} for i in range(25)]
    result = await tool.execute({"_session_id": "s1", "nodes": nodes}, vault=None, db=db)
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0


@pytest.mark.asyncio
async def test_node_missing_required_field_rejected(db):
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {"_session_id": "s1", "nodes": [{"node_id": "A", "role": "dev"}]},  # tanpa prompt
        vault=None,
        db=db,
    )
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0


@pytest.mark.asyncio
async def test_depends_on_wrong_type_rejected(db):
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {
            "_session_id": "s1",
            "nodes": [{"node_id": "A", "role": "dev", "prompt": "x", "depends_on": "not-a-list"}],
        },
        vault=None,
        db=db,
    )
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0


@pytest.mark.asyncio
async def test_cyclic_graph_rejected_zero_db_writes(db):
    """Fail-closed inti: graph cyclic ditolak SEBELUM satu subtask pun mulai —
    tak ada baris task_graphs/task_nodes tertulis sama sekali."""
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {
            "_session_id": "s1",
            "nodes": [
                {"node_id": "A", "role": "dev", "prompt": "a", "depends_on": ["B"]},
                {"node_id": "B", "role": "dev", "prompt": "b", "depends_on": ["A"]},
            ],
        },
        vault=None,
        db=db,
    )
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0
    assert await _count_rows(db, "task_nodes") == 0


@pytest.mark.asyncio
async def test_unknown_role_rejected(db):
    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {"_session_id": "s1", "nodes": [{"node_id": "A", "role": "not-a-role", "prompt": "x"}]},
        vault=None,
        db=db,
    )
    assert "error" in result
    assert await _count_rows(db, "task_graphs") == 0


@pytest.mark.asyncio
async def test_valid_submission_executes_and_persists(db, monkeypatch):
    """Submission valid → graph benar-benar dieksekusi (AgentLoop di-mock) dan
    hasilnya dikembalikan sebagai return value tool ini."""

    class _FakeTurn:
        def __init__(self, content):
            self.content = content

    class FakeAgentLoop:
        def __init__(self, cfg, db=None, config=None, approval=None):
            self.cfg = cfg
            self.history = []

        async def run(self, prompt: str):
            self.history.append(_FakeTurn(f"selesai: {prompt}"))
            yield

    monkeypatch.setattr("tools.task_graph_submit._build_agent", lambda cfg, db: FakeAgentLoop(cfg))

    tool = TaskGraphSubmitTool()
    result = await tool.execute(
        {
            "_session_id": "s1",
            "_user_id": "7",
            "goal": "tes goal",
            "nodes": [{"node_id": "A", "role": "dev", "prompt": "kerjakan sesuatu"}],
        },
        vault=None,
        db=db,
    )

    assert result["status"] == "completed"
    assert result["nodes"]["A"]["status"] == "completed"

    graph_row = await db.fetchone("SELECT * FROM task_graphs LIMIT 1")
    assert graph_row["goal"] == "tes goal"
    assert graph_row["owner_user_id"] == "7"
    assert graph_row["session_id"] == "s1"


@pytest.mark.asyncio
async def test_default_user_id_not_recorded_as_owner(db, monkeypatch):
    """user_id='default' (auth nonaktif) → owner_user_id TETAP None, konsisten
    pola `owner_user_id if user_id != 'default' else None` di seluruh codebase."""

    class FakeAgentLoop:
        def __init__(self, cfg, db=None, config=None, approval=None):
            self.cfg = cfg
            self.history = []

        async def run(self, prompt: str):
            yield

    monkeypatch.setattr("tools.task_graph_submit._build_agent", lambda cfg, db: FakeAgentLoop(cfg))

    tool = TaskGraphSubmitTool()
    await tool.execute(
        {
            "_session_id": "s1",
            "_user_id": "default",
            "nodes": [{"node_id": "A", "role": "dev", "prompt": "x"}],
        },
        vault=None,
        db=db,
    )

    graph_row = await db.fetchone("SELECT owner_user_id FROM task_graphs LIMIT 1")
    assert graph_row["owner_user_id"] is None


def test_registered_and_no_approval():
    from tools import TOOL_REGISTRY

    assert "task_graph_submit" in TOOL_REGISTRY
    assert TOOL_REGISTRY["task_graph_submit"].requires_approval is False
