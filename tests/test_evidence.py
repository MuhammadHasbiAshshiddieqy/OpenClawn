"""Test untuk GET /evidence/{event_id} — Evidence-Based Response (TODO.md § Prioritas 2).

Snapshot policy/skill/guardrail per-turn, disimpan `RoutingAuditor.finalize()` ke
kolom `routing_events.evidence_json`, di-expose read-only lewat endpoint ini.
"""

import asyncio

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace sementara (pola sama test_chat_sessions.py)."""
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


def test_evidence_404_for_unknown_event(client):
    resp = client.get("/evidence/999999")
    assert resp.status_code == 404


def test_evidence_returns_null_when_not_yet_finalized(client):
    """Event ada (log_decision sudah jalan) tapi finalize belum → evidence: null,
    bukan 404 (turn masih berjalan, bukan tidak pernah ada)."""
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
                "role": "pm",
                "has_urgency": 0,
                "needs_stream": 1,
                "is_continuation": 0,
                "soul_upgrade_hit": 0,
            },
            soul_upgrade_hit=False,
        )
        return await RoutingAuditor(web_main.db).log_decision("s1", "pm", "query", route)

    event_id = asyncio.run(_setup())
    resp = client.get(f"/evidence/{event_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_id"] == event_id
    assert data["evidence"] is None


def test_evidence_returns_stored_payload_after_finalize(client):
    """Setelah finalize(evidence=...), endpoint mengembalikan payload persis."""
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

    evidence = {
        "policy": {"provider": "gemini", "model": "gemini-2.5-flash", "complexity": "complex"},
        "memory": ["prd-skill"],
        "guardrail": {"status": "clean", "detail": ""},
    }

    async def _setup():
        route = RouteDecision(
            model="gemini-2.5-flash",
            provider="gemini",
            complexity=Complexity.COMPLEX,
            complexity_score=6,
            reason="test",
            cost_per_1k=0.0,
            dimensions={
                "query_tokens": 5,
                "has_tech_kw": 0,
                "needs_multistep": 1,
                "history_len": 0,
                "role": "pm",
                "has_urgency": 0,
                "needs_stream": 1,
                "is_continuation": 0,
                "soul_upgrade_hit": 1,
            },
            soul_upgrade_hit=True,
        )
        auditor = RoutingAuditor(web_main.db)
        eid = await auditor.log_decision("s2", "pm", "buat pdf prd", route)
        await auditor.finalize(eid, _FakeTurn(), evidence=evidence)
        return eid

    event_id = asyncio.run(_setup())
    resp = client.get(f"/evidence/{event_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "pm"
    assert data["session_id"] == "s2"
    assert data["evidence"] == evidence
