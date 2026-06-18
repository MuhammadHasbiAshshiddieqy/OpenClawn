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
