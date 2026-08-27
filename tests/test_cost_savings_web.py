"""Test untuk GET /metrics/cost-savings dan kartu penghematan di GET /metrics
(TODO.md § Prioritas 9.4)."""

import asyncio
from dataclasses import dataclass

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace sementara (pola sama test_role_metrics.py)."""
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


def _log_event(sid: str, model: str, provider: str, tokens_in: int, tokens_out: int) -> int:
    """Buat satu routing_event lengkap via web_main.db (pola sama test_role_metrics.py)."""
    import web.main as web_main
    from core.audit import RoutingAuditor
    from core.router import RouteDecision, Complexity

    @dataclass
    class _FakeTurn:
        tokens_in: int
        tokens_out: int
        cost_usd: float = 0.0
        latency_ms: int = 100
        fallback_used: bool = False

    async def _setup():
        route = RouteDecision(
            model=model,
            provider=provider,
            complexity=Complexity.CRITICAL,
            complexity_score=8,
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
        eid = await auditor.log_decision(sid, "dev", "q", route)
        await auditor.finalize(eid, _FakeTurn(tokens_in=tokens_in, tokens_out=tokens_out))
        return eid

    return asyncio.run(_setup())


def test_cost_savings_json_empty_initially(client):
    resp = client.get("/metrics/cost-savings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["turns_counted"] == 0
    assert data["is_estimate"] is True


def test_cost_savings_json_reflects_logged_events(client):
    _log_event("s1", "gemma4:e4b", "ollama", 1_000_000, 1_000_000)  # gratis

    data = client.get("/metrics/cost-savings").json()
    assert data["turns_counted"] == 1
    assert data["actual_cost_usd"] == 0.0
    assert data["estimated_savings_usd"] > 0


def test_metrics_page_renders_cost_savings_card(client):
    """GET /metrics (HTML) harus render tanpa error walau ada data cost_savings
    — regresi untuk memastikan template.render tidak melempar KeyError/Undefined."""
    _log_event("s1", "gemini-2.5-flash", "gemini", 1000, 1000)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "Cost Savings".lower() in resp.text.lower() or "penghematan" in resp.text.lower()


def test_metrics_page_renders_with_no_data(client):
    """Halaman /metrics tidak boleh error saat belum ada routing_events sama sekali."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
