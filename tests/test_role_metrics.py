"""Test untuk GET /metrics/roles dan POST /feedback/{event_id} — Runtime
Evaluation Engine (TODO.md § Prioritas 2).
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


def _log_event(sid: str, role: str, query: str) -> int:
    """Buat satu routing_event lengkap (log_decision + finalize) via web_main.db."""
    import web.main as web_main
    from core.audit import RoutingAuditor
    from core.router import RouteDecision, Complexity
    from dataclasses import dataclass

    @dataclass
    class _FakeTurn:
        tokens_in: int = 10
        tokens_out: int = 5
        cost_usd: float = 0.0
        latency_ms: int = 100
        fallback_used: bool = False

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
                "role": role,
                "has_urgency": 0,
                "needs_stream": 1,
                "is_continuation": 0,
                "soul_upgrade_hit": 0,
            },
            soul_upgrade_hit=False,
        )
        auditor = RoutingAuditor(web_main.db)
        eid = await auditor.log_decision(sid, role, query, route)
        await auditor.finalize(eid, _FakeTurn())
        return eid

    return asyncio.run(_setup())


def test_metrics_roles_empty_initially(client):
    resp = client.get("/metrics/roles")
    assert resp.status_code == 200
    assert resp.json() == {"roles": []}


def test_metrics_roles_reflects_logged_events(client):
    _log_event("s1", "pm", "q1")
    _log_event("s2", "dev", "q1")

    resp = client.get("/metrics/roles")
    data = resp.json()["roles"]
    roles = {r["role"] for r in data}
    assert roles == {"pm", "dev"}


def test_feedback_404_for_unknown_event(client):
    resp = client.post("/feedback/999999", data={"rating": "5"})
    assert resp.status_code == 404


def test_feedback_400_for_out_of_range_rating(client):
    event_id = _log_event("s3", "pm", "q")
    resp = client.post(f"/feedback/{event_id}", data={"rating": "6"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_feedback_400_for_non_numeric_rating(client):
    event_id = _log_event("s4", "pm", "q")
    resp = client.post(f"/feedback/{event_id}", data={"rating": "not-a-number"})
    assert resp.status_code == 400


def test_feedback_accepted_and_reflected_in_role_report(client):
    event_id = _log_event("s5", "qa", "q")

    resp = client.post(f"/feedback/{event_id}", data={"rating": "4"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "event_id": event_id, "rating": 4}

    roles = client.get("/metrics/roles").json()["roles"]
    qa_row = [r for r in roles if r["role"] == "qa"][0]
    assert qa_row["avg_human_feedback"] == 4.0
