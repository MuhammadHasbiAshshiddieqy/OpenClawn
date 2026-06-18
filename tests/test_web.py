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
