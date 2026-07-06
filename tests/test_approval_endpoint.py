"""Test untuk GET /approval/{approval_id} — Human Approval Pipeline sebagai node
query-able (TODO.md § Prioritas 2).

approval_id sebelumnya hanya tersirat sebagai substring sementara di kolom
`decision` ("pending:{id}"), hilang setelah keputusan final ditulis. Kolom
`approval_log.approval_id` (baru) + endpoint ini membuatnya query-able mandiri.
"""

import asyncio

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


def test_approval_404_for_unknown_id(client):
    resp = client.get("/approval/unknown-id-xyz")
    assert resp.status_code == 404


def test_approval_returns_pending_status_before_resolve(client):
    """Selagi approval masih menunggu keputusan → decision='pending', bukan 404."""
    import web.main as web_main
    from security.approval import ApprovalGate

    async def _setup():
        gate = ApprovalGate(web_main.db, web_main.CONFIG)
        task = asyncio.create_task(gate.request("s1", "code_run", {"code": "x"}, "aid-1"))
        await asyncio.sleep(0.05)
        gate.resolve("aid-1", True)
        await task

    asyncio.run(_setup())
    resp = client.get("/approval/aid-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "approved"
    assert data["tool_name"] == "code_run"
    assert data["tool_input"] == {"code": "x"}


def test_approval_status_traceable_through_full_lifecycle(client):
    """Regresi inti: approval_id harus TETAP bisa di-query setelah decision
    berubah dari pending -> approved (sebelumnya hilang, hanya tersirat di
    substring 'decision' yang ditimpa)."""
    import web.main as web_main
    from security.approval import ApprovalGate

    async def _setup():
        gate = ApprovalGate(web_main.db, web_main.CONFIG)
        task = asyncio.create_task(
            gate.request("s2", "shell_run", {"command": "ls"}, "aid-lifecycle")
        )
        await asyncio.sleep(0.05)
        gate.resolve("aid-lifecycle", False)
        await task

    asyncio.run(_setup())
    resp = client.get("/approval/aid-lifecycle")
    assert resp.status_code == 200
    assert resp.json()["decision"] == "rejected"
    assert resp.json()["approval_id"] == "aid-lifecycle"
