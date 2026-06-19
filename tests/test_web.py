"""Smoke test untuk Web UI — endpoint /metrics merender calibration tanpa error.

Verifikasi wiring RoutingCalibrator → template (Sprint 4). Tidak memanggil LLM.
"""

import warnings
import pytest

# TestClient via starlette memunculkan DeprecationWarning httpx — bukan error nyata.
warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB sementara agar tidak menyentuh data/openclawn.db asli."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))

    # Import setelah env diset agar CONFIG.from_env() memakai DB sementara.
    import importlib
    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    with TestClient(web_main.app) as c:
        yield c


def test_metrics_renders_empty(client):
    """/metrics tanpa data audit harus render 200 + pesan 'not enough data'."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "Tuning Recommendations" in resp.text
    # Belum ada event → blok 'data belum cukup' atau tabel kosong
    assert "Data audit belum cukup" in resp.text or "Belum ada data" in resp.text


def test_index_renders(client):
    """Halaman chat utama harus render 200."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_index_lists_new_roles(client):
    """Role baru (data, security) muncul di sidebar + chip peserta."""
    html = client.get("/").text
    assert 'data-role="data"' in html
    assert 'data-role="security"' in html
    assert "/?role=data" in html
    assert "/?role=security" in html


def test_index_unknown_role_falls_back(client):
    """?role= tak dikenal tidak crash; fallback ke role pertama."""
    resp = client.get("/?role=ghost")
    assert resp.status_code == 200


def test_approve_requires_valid_params(client):
    """/approve tanpa approval_id valid harus return ok=False, tidak crash."""
    resp = client.post("/approve", data={"decision": "approve"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_approvals_empty(client):
    """/approvals tanpa pending harus return list kosong."""
    resp = client.get("/approvals")
    assert resp.status_code == 200
    assert resp.json() == {"pending": []}


def test_metrics_shows_active_offset(client):
    """/metrics menampilkan offset threshold aktif (loop tertutup #1)."""
    html = client.get("/metrics").text
    assert "Offset threshold aktif" in html


def test_calibration_apply_then_revert_roundtrip(client):
    """Apply menggeser offset (tercermin di /metrics), revert mengembalikannya."""
    # Apply -1: redirect ke /metrics (TestClient mengikuti redirect → 200).
    resp = client.post("/calibration/apply", data={"delta": "-1", "reason": "uji"})
    assert resp.status_code == 200
    # Offset aktif kini -1 → pill menampilkan tanda & teks "naik tier lebih cepat".
    assert "naik tier lebih cepat" in resp.text
    assert "Riwayat Kalibrasi" in resp.text  # baris audit muncul

    # Revert: kembali ke 0 → teks "belum dikalibrasi".
    resp = client.post("/calibration/revert")
    assert resp.status_code == 200
    assert "belum dikalibrasi" in resp.text


def test_calibration_apply_zero_delta_is_noop(client):
    """delta=0 tidak mengubah offset, tetap redirect 200 tanpa baris audit."""
    resp = client.post("/calibration/apply", data={"delta": "0"})
    assert resp.status_code == 200
    assert "belum dikalibrasi" in resp.text


def test_skills_page_renders_empty(client):
    """/skills tanpa skill harus render 200 + pesan kosong + count chip nol."""
    resp = client.get("/skills")
    assert resp.status_code == 200
    assert "Skill Decay" in resp.text
    assert "Belum ada skill" in resp.text


def test_skills_page_shows_seeded_skill(client, tmp_path, monkeypatch):
    """Skill di DB muncul di tabel dengan status & nama. Verifikasi wiring read.

    Seed lewat koneksi sqlite3 sinkron langsung ke file DB (path dari env), agar
    tidak bentrok dengan event loop milik TestClient.
    """
    import os
    import sqlite3

    db_file = os.environ["OPENCLAWN_DB"]
    conn = sqlite3.connect(db_file)
    conn.execute(
        """INSERT INTO skills (role, skill_name, skill_content, status,
           confidence, use_count, decay_score, last_used_at)
           VALUES ('dev', 'parse_csv', 'isi', 'active', 4.0, 3, 0.9, datetime('now'))"""
    )
    conn.commit()
    conn.close()

    html = client.get("/skills").text
    assert "parse_csv" in html
    assert "status-active" in html


def test_skills_page_shows_crystallization_attempt(client):
    """Percobaan kristalisasi (Inovasi 3) tampil di /skills dengan keputusan evaluator."""
    import os
    import sqlite3

    conn = sqlite3.connect(os.environ["OPENCLAWN_DB"])
    conn.execute(
        """INSERT INTO crystallization_log
           (role, skill_name, generator_model, evaluator_model, confidence,
            critical_gaps, status, reasoning)
           VALUES ('dev','build_api','claude-sonnet-4-6','claude-sonnet-4-6',5,0,'active','solid')"""
    )
    conn.commit()
    conn.close()

    html = client.get("/skills").text
    assert "Kristalisasi" in html
    assert "build_api" in html
    assert "solid" in html


def test_conversations_page_renders_empty(client):
    """/conversations tanpa arsip → 200 + pesan kosong."""
    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert "Conversations" in resp.text
    assert "Belum ada percakapan" in resp.text


def test_conversations_page_shows_archived_run(client):
    """Percakapan tersimpan tampil dengan pattern, peserta, dan transkrip."""
    import json
    import os
    import sqlite3

    conn = sqlite3.connect(os.environ["OPENCLAWN_DB"])
    conn.execute(
        """INSERT INTO conversations (session_id, pattern, participants, initial_message,
               transcript_json, turns, end_reason, cost_usd)
           VALUES ('s1','debate','pm,dev','adu argumen',?,2,'strategy_done',0.0)""",
        (json.dumps([["user", "adu argumen"], ["pm", "setuju"], ["dev", "tidak"]]),),
    )
    conn.commit()
    conn.close()

    html = client.get("/conversations").text
    assert "debate" in html
    assert "pm,dev" in html
    assert "adu argumen" in html
    assert "setuju" in html


# ── /converse/* (multi-agent conversation endpoints) ──────────────────────────


def test_converse_interject_unknown_session(client):
    """Interject ke sesi yang tidak aktif → ok=False, tidak crash."""
    resp = client.post("/converse/interject", data={"session_id": "ghost", "message": "halo"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_converse_stop_unknown_session(client):
    """Stop ke sesi yang tidak aktif → ok=False, tidak crash."""
    resp = client.post("/converse/stop", data={"session_id": "ghost"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_converse_interject_and_stop_reach_live_control(client):
    """Interject & stop mencapai ConversationControl yang terdaftar di registry."""
    import web.main as web_main
    from core.conversation import ConversationControl

    control = ConversationControl()
    web_main._conversations["live-1"] = control
    try:
        r1 = client.post("/converse/interject", data={"session_id": "live-1", "message": "fokus"})
        assert r1.json()["ok"] is True
        assert control.pop_interjection() == "fokus"

        r2 = client.post("/converse/stop", data={"session_id": "live-1"})
        assert r2.json()["ok"] is True
        # stop() memicu flag internal → is_stopped() akan True pada cek berikutnya.
        assert control._stopped is True
    finally:
        web_main._conversations.pop("live-1", None)


def test_converse_stream_emits_named_frames(client, monkeypatch):
    """/converse/stream menstream frame SSE bernama (turn/token/conversation_end).

    Orchestrator di-mock agar tidak memanggil LLM nyata — kita hanya memverifikasi
    wiring endpoint → frame SSE, bukan logika percakapan (itu di test_conversation).
    """
    import web.main as web_main
    from core.conversation import ConversationEvent

    class FakeOrch:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, initial_message):
            yield ConversationEvent("turn", role="pm", text="PM", turn_index=0)
            yield ConversationEvent("token", role="pm", text="halo dari pm")
            yield ConversationEvent("conversation_end", detail="strategy_done", usage={})

    monkeypatch.setattr(web_main, "ConversationOrchestrator", FakeOrch)

    resp = client.post(
        "/converse/stream",
        data={"message": "bangun fitur", "pattern": "pipeline", "session_id": "s-stream"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "event: turn" in body
    assert "event: token" in body
    assert "halo dari pm" in body
    assert "event: conversation_end" in body
    assert "strategy_done" in body
    assert "event: done" in body


def test_converse_stream_rejects_unknown_pattern(client):
    """Pattern tak dikenal → frame error, bukan crash 500."""
    resp = client.post(
        "/converse/stream",
        data={"message": "x", "pattern": "nonsense", "session_id": "s-bad"},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text


def test_router_page_renders_tiers(client):
    """/router menampilkan 5 tier dengan dropdown model + tanda default."""
    html = client.get("/router").text
    assert "Router Model Map" in html
    for tier in ("TRIVIAL", "SIMPLE", "MODERATE", "COMPLEX", "CRITICAL"):
        assert tier in html
    assert "tier_trivial" in html  # nama field form
    assert "default" in html  # tanda peta default aktif


def test_router_save_then_reflected(client):
    """Simpan peta → tier terpilih berubah di /router; reset → kembali default."""
    resp = client.post("/router", data={"tier_trivial": "gemini|gemini-2.0-flash"})
    assert resp.status_code == 200
    assert "Peta kustom aktif" in resp.text

    resp = client.post("/router", data={"action": "reset"})
    assert resp.status_code == 200
    assert "memakai peta default" in resp.text
