"""Test untuk GET /audit/verify (diperluas dengan anchors) dan POST /audit/anchor
(TODO.md § Prioritas 9.1 follow-up)."""

import asyncio

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace + anchor path sementara (pola sama
    test_cost_savings_web.py / test_role_metrics.py)."""
    import importlib

    db_file = tmp_path / "test.db"
    anchor_file = tmp_path / "anchors.jsonl"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENCLAWN_AUDIT_ANCHOR_PATH", str(anchor_file))

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    with TestClient(web_main.app) as c:
        yield c


def _log_decision():
    """Buat satu entry di audit_chain via web_main.db (pola sama test_role_metrics.py)."""
    import web.main as web_main
    from core.audit import RoutingAuditor
    from core.router import RouteDecision, Complexity

    async def _setup():
        route = RouteDecision(
            model="gemma4:e4b",
            provider="ollama",
            complexity=Complexity.SIMPLE,
            complexity_score=2,
            reason="test",
            cost_per_1k=0.0,
            dimensions={
                "query_tokens": 5,
                "has_tech_kw": 0,
                "needs_multistep": 0,
                "history_len": 0,
                "role": "dev",
                "has_urgency": 0,
                "needs_stream": 1,
                "is_continuation": 0,
                "soul_upgrade_hit": 0,
            },
            soul_upgrade_hit=False,
        )
        auditor = RoutingAuditor(web_main.db)
        await auditor.log_decision("s1", "dev", "q", route)

    asyncio.run(_setup())


def test_audit_verify_includes_anchors_field(client):
    """GET /audit/verify HARUS menyertakan hasil verifikasi anchor, bukan cuma
    verify() rantai saja."""
    resp = client.get("/audit/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert "anchors" in data
    assert data["anchors"]["anchors_checked"] == 0  # belum pernah di-anchor
    assert data["anchors"]["ok"] is True


def test_audit_anchor_post_writes_when_activity_exists(client):
    _log_decision()

    resp = client.post("/audit/anchor")
    assert resp.status_code == 200
    data = resp.json()
    assert data["written"] is True
    assert data["anchor"]["id"] == 1


def test_audit_anchor_post_no_op_without_new_activity(client):
    _log_decision()
    client.post("/audit/anchor")  # anchor pertama

    resp = client.post("/audit/anchor")  # tanpa aktivitas baru
    data = resp.json()
    assert data["written"] is False
    assert data["anchor"] is None


def test_audit_verify_reflects_anchor_after_posting(client):
    _log_decision()
    client.post("/audit/anchor")

    resp = client.get("/audit/verify")
    data = resp.json()
    assert data["anchors"]["ok"] is True
    assert data["anchors"]["anchors_checked"] == 1
