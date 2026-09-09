"""Test untuk core/task_executor.py — eksekusi DAG sungguhan (§ Task Graph,
Fase 2: concurrency engine + fault containment).

`AgentLoop` di-mock TOTAL (CLAUDE.md §5: LLM selalu di-mock) — `agent_factory`
mengembalikan objek `FakeAgentLoop` palsu, bukan `AgentLoop` sungguhan.
"""

import asyncio
import time

import pytest

from core.task_executor import TaskGraphExecutor
from core.task_graph import TaskGraph, TaskNode
from infra.config import AppConfig
from infra.database import DatabaseManager


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


class _FakeTurn:
    def __init__(self, content: str):
        self.content = content


class FakeAgentLoop:
    """Pengganti `AgentLoop` — `run()` async generator, `.history` list Turn-like.

    `behavior` per node_id: "ok" (sukses langsung), "fail" (selalu raise),
    "fail_once" (raise di percobaan pertama, sukses berikutnya), atau angka
    (detik `asyncio.sleep` sebelum sukses — untuk uji konkurensi/overlap).
    """

    _attempt_counts: dict[str, int] = {}

    def __init__(self, cfg, behavior, tracker=None):
        self.cfg = cfg
        self.behavior = behavior
        self.tracker = tracker
        self.history = []

    async def run(self, prompt: str):
        node_id = self.cfg.node_id
        FakeAgentLoop._attempt_counts[node_id] = FakeAgentLoop._attempt_counts.get(node_id, 0) + 1
        attempt = FakeAgentLoop._attempt_counts[node_id]

        if self.tracker is not None:
            self.tracker.append((node_id, "start", time.monotonic()))

        if isinstance(self.behavior, (int, float)):
            await asyncio.sleep(self.behavior)
        elif self.behavior == "fail":
            raise RuntimeError(f"{node_id} sengaja gagal")
        elif self.behavior == "fail_once" and attempt == 1:
            raise RuntimeError(f"{node_id} gagal percobaan pertama")

        if self.tracker is not None:
            self.tracker.append((node_id, "end", time.monotonic()))
        self.history.append(_FakeTurn(f"hasil {node_id}"))
        yield  # generator kosong — cukup untuk `async for _ in agent.run(...)`


def _make_config(**overrides) -> AppConfig:
    defaults = dict(
        db_path=":memory:",
        task_graph_max_concurrency=3,
        task_graph_max_node_attempts=2,
        task_graph_retry_backoff_sec=0.01,
        task_graph_node_timeout_sec=5,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


@pytest.mark.asyncio
async def test_all_nodes_succeed_graph_completed(db):
    FakeAgentLoop._attempt_counts = {}
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
    ]
    graph = TaskGraph(nodes)
    config = _make_config()
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, "ok"))

    result = await executor.run("t1", graph, owner_user_id=None, parent_session_id="s1")

    assert result["status"] == "completed"
    assert result["nodes"]["A"]["status"] == "completed"
    assert result["nodes"]["B"]["status"] == "completed"
    assert result["nodes"]["A"]["result_summary"] == "hasil A"

    row = await db.fetchone("SELECT status FROM task_graphs WHERE id='t1'")
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_independent_nodes_run_concurrently(db):
    """Dua node TANPA depends_on harus overlap eksekusinya — bukan sekuensial."""
    FakeAgentLoop._attempt_counts = {}
    tracker: list[tuple[str, str, float]] = []
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b"),
    ]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_concurrency=2)
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, 0.05, tracker=tracker))

    await executor.run("t2", graph, owner_user_id=None, parent_session_id="s1")

    starts = {nid: ts for nid, ev, ts in tracker if ev == "start"}
    ends = {nid: ts for nid, ev, ts in tracker if ev == "end"}
    # Overlap: B mulai SEBELUM A selesai (atau sebaliknya) — kalau sekuensial,
    # start kedua akan >= end pertama.
    assert starts["B"] < ends["A"] or starts["A"] < ends["B"]


@pytest.mark.asyncio
async def test_max_concurrency_respected(db):
    """5 node independen, max_concurrency=2 — tak pernah lebih dari 2 jalan bersamaan."""
    FakeAgentLoop._attempt_counts = {}
    concurrent_count = 0
    peak = 0
    lock = asyncio.Lock()

    class TrackingFakeAgentLoop(FakeAgentLoop):
        async def run(self, prompt: str):
            nonlocal concurrent_count, peak
            async with lock:
                concurrent_count += 1
                peak = max(peak, concurrent_count)
            await asyncio.sleep(0.02)
            async with lock:
                concurrent_count -= 1
            self.history.append(_FakeTurn("ok"))
            yield

    nodes = [TaskNode(node_id=f"N{i}", role="dev", prompt="x") for i in range(5)]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_concurrency=2)
    executor = TaskGraphExecutor(db, config, lambda cfg: TrackingFakeAgentLoop(cfg, "ok"))

    result = await executor.run("t3", graph, owner_user_id=None, parent_session_id="s1")

    assert peak <= 2
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_node_exhausts_retries_then_failed(db):
    FakeAgentLoop._attempt_counts = {}
    nodes = [TaskNode(node_id="A", role="dev", prompt="a")]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_node_attempts=3)
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, "fail"))

    result = await executor.run("t4", graph, owner_user_id=None, parent_session_id="s1")

    assert result["status"] == "failed"
    assert result["nodes"]["A"]["status"] == "failed"
    assert "sengaja gagal" in result["nodes"]["A"]["error"]
    assert FakeAgentLoop._attempt_counts["A"] == 3


@pytest.mark.asyncio
async def test_node_succeeds_after_retry(db):
    FakeAgentLoop._attempt_counts = {}
    nodes = [TaskNode(node_id="A", role="dev", prompt="a")]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_node_attempts=3)
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, "fail_once"))

    result = await executor.run("t5", graph, owner_user_id=None, parent_session_id="s1")

    assert result["status"] == "completed"
    assert FakeAgentLoop._attempt_counts["A"] == 2


@pytest.mark.asyncio
async def test_failed_node_blocks_dependents_not_whole_graph(db):
    """A gagal permanen -> B (depends_on A) blocked. C (independen) tetap completed.
    Graph keseluruhan 'partial', bukan 'failed' total — sebagian berhasil tetap
    dihargai sebagai hasil nyata, bukan dibuang."""
    FakeAgentLoop._attempt_counts = {}
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
        TaskNode(node_id="C", role="dev", prompt="c"),
    ]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_node_attempts=1)

    def factory(cfg):
        behavior = "fail" if cfg.node_id == "A" else "ok"
        return FakeAgentLoop(cfg, behavior)

    executor = TaskGraphExecutor(db, config, factory)
    result = await executor.run("t6", graph, owner_user_id=None, parent_session_id="s1")

    assert result["status"] == "partial"
    assert result["nodes"]["A"]["status"] == "failed"
    assert result["nodes"]["B"]["status"] == "blocked"
    assert result["nodes"]["C"]["status"] == "completed"
    # B tak pernah benar-benar dijalankan.
    assert "B" not in FakeAgentLoop._attempt_counts


@pytest.mark.asyncio
async def test_retry_backoff_is_awaited(db, monkeypatch):
    FakeAgentLoop._attempt_counts = {}
    sleep_calls = []
    original_sleep = asyncio.sleep

    async def _spy_sleep(seconds):
        sleep_calls.append(seconds)
        await original_sleep(0)  # jangan benar-benar tunggu di test

    monkeypatch.setattr("core.task_executor.asyncio.sleep", _spy_sleep)

    nodes = [TaskNode(node_id="A", role="dev", prompt="a")]
    graph = TaskGraph(nodes)
    config = _make_config(task_graph_max_node_attempts=3, task_graph_retry_backoff_sec=2.0)
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, "fail"))

    await executor.run("t7", graph, owner_user_id=None, parent_session_id="s1")

    # Percobaan 1 gagal -> sleep(2.0 * 2^0=2.0); percobaan 2 gagal -> sleep(2.0*2^1=4.0);
    # percobaan 3 gagal -> permanen, tak ada sleep lagi setelahnya.
    assert sleep_calls == [2.0, 4.0]


@pytest.mark.asyncio
async def test_task_nodes_persisted_to_db(db):
    """Baris task_graphs/task_nodes benar-benar tertulis, bukan cuma di memori."""
    FakeAgentLoop._attempt_counts = {}
    nodes = [TaskNode(node_id="A", role="dev", prompt="halo dunia")]
    graph = TaskGraph(nodes)
    config = _make_config()
    executor = TaskGraphExecutor(db, config, lambda cfg: FakeAgentLoop(cfg, "ok"))

    await executor.run("t8", graph, owner_user_id="42", parent_session_id="s-parent", goal="tes")

    graph_row = await db.fetchone("SELECT * FROM task_graphs WHERE id='t8'")
    assert graph_row["goal"] == "tes"
    assert graph_row["owner_user_id"] == "42"
    assert graph_row["session_id"] == "s-parent"
    assert graph_row["status"] == "completed"

    node_row = await db.fetchone("SELECT * FROM task_nodes WHERE task_id='t8' AND node_id='A'")
    assert node_row["role"] == "dev"
    assert node_row["prompt"] == "halo dunia"
    assert node_row["status"] == "completed"
    assert node_row["session_id"] == "t8:A"
    assert node_row["result_summary"] == "hasil A"
