"""Test untuk GET /tasks/{task_id} dan GET /tasks/{task_id}/timeline — Task Graph
observability/replay (§ Task Graph, TODO.md § Prioritas 12 Fase 4).

Kepemilikan lintas-user diuji terpisah di tests/test_rbac_web.py (reuse fixture
client_oidc yang sudah ada di sana) — file ini fokus ke bentuk response & join
timeline, tanpa auth (pola sama tests/test_evidence.py).
"""

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace sementara (pola sama test_evidence.py)."""
    import importlib

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    with TestClient(web_main.app) as c:
        yield c


async def _seed_graph(db, task_id="t1", owner_user_id=None):
    await db.execute(
        """INSERT INTO task_graphs (id, goal, owner_user_id, session_id, status)
           VALUES (?, 'tes goal', ?, 'parent-s1', 'completed')""",
        (task_id, owner_user_id),
    )
    await db.execute(
        """INSERT INTO task_nodes
           (task_id, node_id, role, prompt, depends_on_json, status, result_summary, session_id)
           VALUES (?, 'A', 'dev', 'kerjakan A', '[]', 'completed', 'hasil A', ?)""",
        (task_id, f"{task_id}:A"),
    )
    await db.execute(
        """INSERT INTO task_nodes
           (task_id, node_id, role, prompt, depends_on_json, status, result_summary, session_id)
           VALUES (?, 'B', 'dev', 'kerjakan B', '["A"]', 'completed', 'hasil B', ?)""",
        (task_id, f"{task_id}:B"),
    )


def test_get_task_unknown_returns_404(client):
    resp = client.get("/tasks/does-not-exist")
    assert resp.status_code == 404


def test_get_task_timeline_unknown_returns_404(client):
    resp = client.get("/tasks/does-not-exist/timeline")
    assert resp.status_code == 404


def test_get_task_returns_graph_and_nodes(client):
    import asyncio

    import web.main as web_main

    asyncio.run(_seed_graph(web_main.db, task_id="t1"))

    resp = client.get("/tasks/t1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t1"
    assert body["goal"] == "tes goal"
    assert body["status"] == "completed"
    assert len(body["nodes"]) == 2
    by_id = {n["node_id"]: n for n in body["nodes"]}
    assert by_id["A"]["result_summary"] == "hasil A"
    assert by_id["B"]["depends_on"] == ["A"]


def test_get_task_timeline_merges_three_sources_sorted(client):
    import asyncio

    import web.main as web_main

    async def _seed():
        db = web_main.db
        await _seed_graph(db, task_id="t2")
        await db.execute(
            """INSERT INTO routing_events
               (session_id, role, query_text, model_chosen, provider, complexity_label,
                task_id, node_id, created_at)
               VALUES ('t2:A', 'dev', 'q', 'gemma4:e4b', 'ollama', 'simple', 't2', 'A',
                       '2026-01-01 00:00:01')"""
        )
        await db.execute(
            """INSERT INTO tool_invocations
               (session_id, role, tool_name, outcome, latency_ms, task_id, node_id, created_at)
               VALUES ('t2:A', 'dev', 'file_read', 'ok', 5, 't2', 'A', '2026-01-01 00:00:02')"""
        )
        await db.execute(
            """INSERT INTO approval_log
               (session_id, tool_name, decision, task_id, node_id, created_at)
               VALUES ('t2:A', 'code_run', 'proposal:pending', 't2', 'A', '2026-01-01 00:00:03')"""
        )

    asyncio.run(_seed())

    resp = client.get("/tasks/t2/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t2"
    kinds = [e["kind"] for e in body["timeline"]]
    assert kinds == ["routing", "tool", "approval"]  # urut waktu, bukan urut tabel
    assert all(e["node_id"] == "A" for e in body["timeline"])


def test_get_task_timeline_empty_for_task_with_no_events(client):
    import asyncio

    import web.main as web_main

    asyncio.run(_seed_graph(web_main.db, task_id="t3"))

    resp = client.get("/tasks/t3/timeline")
    assert resp.status_code == 200
    assert resp.json()["timeline"] == []
