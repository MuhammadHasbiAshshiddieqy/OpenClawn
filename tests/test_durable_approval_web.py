"""POST /approve & GET /approvals lintas 'restart' server (§ Durable execution,
TODO.md Prioritas 8.1) — endpoint-level, pelengkap `tests/test_durable_approval.py`
yang menguji `ApprovalGate`/`core.late_execute` langsung.

Simulasi restart: `web_main.approval_gate` yang dipakai `TestClient` TIDAK PERNAH
diisi manual (tak ada `.request()` dipanggil) — baris pending disuntik langsung ke
DB, persis kondisi proses baru yang membaca `approval_log` peninggalan proses lama.
"""

import json

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
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


async def _insert_orphan(session_id, role, tool_name, tool_input, approval_id, owner=None):
    """`workdir` (§ working directory adaptif): di-set eksplisit ke `OPENCLAWN_WORKSPACE`
    (fixture `client` di atas) lewat `session_workspace` — bukan mengandalkan
    `CONFIG.workspace_root` global, yang jadi TAK deterministik di test karena
    `importlib.reload(config_mod)` membuat objek `CONFIG` baru sementara modul
    tool (`tools/file_ops.py`, di-import sekali per proses pytest) tetap
    memegang referensi objek `CONFIG` LAMA dari test lain sebelumnya."""
    import os

    import web.main as web_main
    from infra.workspace import SessionWorkspaceStore

    await web_main.db.execute(
        "INSERT INTO chat_sessions (session_id, role) VALUES (?, ?)", (session_id, role)
    )
    await web_main.db.execute(
        """INSERT INTO approval_log
           (session_id, tool_name, tool_input, decision, approval_id, owner_user_id)
           VALUES (?,?,?,?,?,?)""",
        (session_id, tool_name, json.dumps(tool_input), "pending", approval_id, owner),
    )
    await SessionWorkspaceStore(web_main.db).set(session_id, os.environ["OPENCLAWN_WORKSPACE"])


def test_get_approvals_shows_orphan_after_simulated_restart(client):
    import asyncio

    asyncio.run(
        _insert_orphan(
            "s-web-1", "dev", "file_write", {"path": "a.txt", "content": "hi"}, "web-orph-1"
        )
    )
    resp = client.get("/approvals")
    assert resp.status_code == 200
    pending = resp.json()["pending"]
    assert any(p["approval_id"] == "web-orph-1" and p.get("orphan") for p in pending)


def test_approve_orphan_actually_executes_tool(client, tmp_path):
    import asyncio

    asyncio.run(
        _insert_orphan(
            "s-web-2", "dev", "file_write", {"path": "b.txt", "content": "world"}, "web-orph-2"
        )
    )
    resp = client.post("/approve", data={"approval_id": "web-orph-2", "decision": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["executed"] is True
    assert body["result"]["ok"] is True
    assert (tmp_path / "b.txt").read_text() == "world"

    status = client.get("/approval/web-orph-2")
    assert status.json()["decision"] == "approved:late"


def test_reject_orphan_marks_rejected_without_executing(client, tmp_path):
    import asyncio

    asyncio.run(
        _insert_orphan(
            "s-web-3", "dev", "file_write", {"path": "c.txt", "content": "nope"}, "web-orph-3"
        )
    )
    resp = client.post("/approve", data={"approval_id": "web-orph-3", "decision": "reject"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["decision"] == "rejected"
    assert not (tmp_path / "c.txt").exists()

    status = client.get("/approval/web-orph-3")
    assert status.json()["decision"] == "rejected"


def test_approve_unknown_approval_id_returns_error(client):
    resp = client.post("/approve", data={"approval_id": "does-not-exist", "decision": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
