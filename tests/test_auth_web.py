"""Test end-to-end untuk auth middleware di web/main.py (bukan unit security/auth.py).

Verifikasi perilaku HTTP nyata: redirect ke /login saat auth aktif tanpa sesi,
login sukses set cookie, CSRF menolak POST tanpa token, endpoint publik tetap
bisa diakses tanpa sesi.
"""

import time
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _make_client(
    tmp_path, monkeypatch, auth_token: str | None = None, idle_timeout_sec: int | None = None
):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    if auth_token is not None:
        monkeypatch.setenv("OPENCLAWN_AUTH_TOKEN", auth_token)
    else:
        monkeypatch.delenv("OPENCLAWN_AUTH_TOKEN", raising=False)
    if idle_timeout_sec is not None:
        monkeypatch.setenv("OPENCLAWN_IDLE_TIMEOUT_SEC", str(idle_timeout_sec))
    else:
        monkeypatch.delenv("OPENCLAWN_IDLE_TIMEOUT_SEC", raising=False)

    import importlib

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    return TestClient(web_main.app)


@pytest.fixture
def client_no_auth(tmp_path, monkeypatch):
    """Auth nonaktif (default) — perilaku lama, tak ada login."""
    with _make_client(tmp_path, monkeypatch, auth_token=None) as c:
        yield c


@pytest.fixture
def client_auth(tmp_path, monkeypatch):
    """Auth aktif dengan token 'test-secret-token'."""
    with _make_client(tmp_path, monkeypatch, auth_token="test-secret-token") as c:
        yield c


@pytest.fixture
def client_auth_idle(tmp_path, monkeypatch):
    """Auth aktif + idle timeout 60 detik — untuk test refresh cookie sliding."""
    with _make_client(
        tmp_path, monkeypatch, auth_token="test-secret-token", idle_timeout_sec=60
    ) as c:
        yield c


# ── Auth nonaktif: perilaku lama tak berubah ─────────────────────────────────


def test_no_auth_root_accessible_without_session(client_no_auth):
    resp = client_no_auth.get("/")
    assert resp.status_code == 200


def test_no_auth_health_reports_auth_disabled(client_no_auth):
    resp = client_no_auth.get("/health")
    assert resp.json()["auth_enabled"] is False


# ── Auth aktif: proteksi & login flow ────────────────────────────────────────


def test_auth_enabled_redirects_unauthenticated_get_to_login(client_auth):
    resp = client_auth.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_auth_enabled_unauthenticated_post_returns_401_json(client_auth):
    resp = client_auth.post("/settings", data={"model_choice": "auto"})
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


def test_health_and_login_reachable_without_session(client_auth):
    assert client_auth.get("/health").status_code == 200
    assert client_auth.get("/login").status_code == 200


def test_metrics_prometheus_reachable_without_session(client_auth):
    """TODO.md § Prioritas 6: scraper Prometheus tak bawa cookie sesi, endpoint
    ini harus tetap public walau auth aktif — sama pola /health."""
    resp = client_auth.get("/metrics/prometheus")
    assert resp.status_code == 200


def test_static_reachable_without_session(client_auth):
    resp = client_auth.get("/static/style.css")
    assert resp.status_code == 200


def test_login_wrong_token_rejected(client_auth):
    resp = client_auth.post(
        "/login", data={"token": "wrong-password", "next": "/"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=true" in resp.headers["location"]
    assert "openclawn_session" not in resp.cookies


def test_login_correct_token_sets_cookies_and_grants_access(client_auth):
    resp = client_auth.post(
        "/login",
        data={"token": "test-secret-token", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "openclawn_session" in resp.cookies
    assert "openclawn_csrf" in resp.cookies

    # Sesi valid → halaman utama sekarang bisa diakses.
    resp2 = client_auth.get("/")
    assert resp2.status_code == 200


def test_login_rejects_open_redirect_via_next(client_auth):
    """`next` yang menunjuk domain eksternal harus dinetralkan ke '/' (cegah open redirect)."""
    resp = client_auth.post(
        "/login",
        data={"token": "test-secret-token", "next": "https://evil.example.com/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_csrf_missing_token_rejected_after_login(client_auth):
    """Login sukses lalu POST form TANPA csrf_token → 403, bukan diproses."""
    client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    resp = client_auth.post("/settings", data={"model_choice": "auto", "compaction_mode": "off"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "csrf_failed"


def test_csrf_valid_token_allows_post(client_auth):
    """Login lalu kirim csrf_token cookie yang sama sebagai field form → diterima."""
    login_resp = client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    csrf_value = login_resp.cookies.get("openclawn_csrf")
    assert csrf_value

    resp = client_auth.post(
        "/settings",
        data={"model_choice": "auto", "compaction_mode": "off", "csrf_token": csrf_value},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # redirect sukses, bukan 403


def test_csrf_exempt_paths_bypass_check(client_auth):
    """Endpoint SSE/fetch (/answer) tetap diproses tanpa csrf_token selama sesi valid."""
    client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    resp = client_auth.post("/answer", data={"session_id": "nonexistent", "answer": "hi"})
    assert resp.status_code != 403


def test_logout_clears_session(client_auth):
    login_resp = client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    csrf_value = login_resp.cookies.get("openclawn_csrf")
    resp = client_auth.post("/logout", data={"csrf_token": csrf_value}, follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_logout_without_csrf_rejected(client_auth):
    """Logout TANPA csrf_token juga harus ditolak — form nyata di UI selalu menyertakannya."""
    client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    resp = client_auth.post("/logout")
    assert resp.status_code == 403


# ── Idle timeout (opt-in, TODO.md § Prioritas 1.5) ───────────────────────────


def test_idle_timeout_off_does_not_refresh_cookie(client_auth):
    """Default (idle_timeout_sec=None) — cookie TIDAK di-refresh tiap request."""
    login_resp = client_auth.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    original_cookie = login_resp.cookies.get("openclawn_session")

    resp = client_auth.get("/")
    assert resp.status_code == 200
    assert "openclawn_session" not in resp.cookies  # tak ada Set-Cookie baru
    assert client_auth.cookies.get("openclawn_session") == original_cookie


def test_idle_timeout_on_refreshes_cookie_each_valid_request(client_auth_idle):
    """idle_timeout_sec diset → tiap request valid menerbitkan ulang cookie sesi."""
    login_resp = client_auth_idle.post(
        "/login", data={"token": "test-secret-token", "next": "/"}, follow_redirects=False
    )
    assert "openclawn_session" in login_resp.cookies

    resp = client_auth_idle.get("/")
    assert resp.status_code == 200
    assert "openclawn_session" in resp.cookies  # Set-Cookie baru di respons


def test_idle_timeout_rejects_session_older_than_idle_window():
    """Token yang usianya melewati idle_timeout_sec (tapi masih dalam absolute
    expiry 7 hari) harus ditolak — inti idle timeout: idle, bukan absolute."""
    from security.auth import _sign, verify_session_token

    old_ts = str(int(time.time()) - 120)  # 120 detik lalu
    payload = f"{old_ts}."
    token = f"{payload}.{_sign(payload, 'test-secret-token')}"

    # Idle window 60 detik → token berusia 120 detik ditolak walau absolute expiry (7 hari) belum lewat.
    assert verify_session_token(token, "test-secret-token", max_age_sec=60) == (False, None)
    # Tanpa idle timeout (absolute expiry biasa) token yang sama masih valid.
    assert verify_session_token(token, "test-secret-token") == (True, None)


# ── Cookie Secure flag (audit produksi 2026-07-30) ────────────────────────────


def _make_scope(scheme: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    """Scope ASGI minimal tapi LENGKAP — Starlette `Request.url` butuh
    `query_string`/`server`/dst untuk membangun URL dengan benar; scope
    sebagian (tanpa field ini) diam-diam membuat `url.scheme` kosong terlepas
    dari `scope['scheme']`, ditemukan saat menulis test ini sendiri."""
    return {
        "type": "http",
        "scheme": scheme,
        "headers": headers or [],
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "server": ("testserver", 443 if scheme == "https" else 80),
        "client": ("testclient", 12345),
        "root_path": "",
        "http_version": "1.1",
    }


def test_is_secure_request_plain_http():
    """Request HTTP biasa (tanpa X-Forwarded-Proto) → tidak secure."""
    from starlette.requests import Request

    from web.main import _is_secure_request

    assert _is_secure_request(Request(_make_scope("http"))) is False


def test_is_secure_request_trusts_x_forwarded_proto():
    """Audit produksi 2026-07-29/30: uvicorn (Dockerfile.role, tanpa
    --proxy-headers) selalu lihat scheme http walau Caddy men-terminasi TLS di
    depan (SATU-SATUNYA topologi produksi yang direkomendasikan, lihat
    Caddyfile.example) — X-Forwarded-Proto dari reverse_proxy Caddy harus
    dipercaya untuk flag Secure cookie."""
    from starlette.requests import Request

    from web.main import _is_secure_request

    scope = _make_scope("http", headers=[(b"x-forwarded-proto", b"https")])
    assert _is_secure_request(Request(scope)) is True


def test_is_secure_request_true_for_direct_https():
    """Deployment yang benar-benar HTTPS langsung di ASGI (bukan lewat proxy
    plain-HTTP) tetap terdeteksi tanpa butuh header."""
    from starlette.requests import Request

    from web.main import _is_secure_request

    assert _is_secure_request(Request(_make_scope("https"))) is True


def test_login_sets_secure_cookie_when_forwarded_proto_https(client_auth):
    """End-to-end: login lewat client yang mengirim X-Forwarded-Proto: https
    (mensimulasikan koneksi asli dari Caddy) → Set-Cookie sesi punya flag Secure."""
    resp = client_auth.post(
        "/login",
        data={"token": "test-secret-token", "next": "/"},
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    set_cookie_headers = resp.headers.get_list("set-cookie")
    session_cookie_header = next(
        h for h in set_cookie_headers if h.startswith("openclawn_session=")
    )
    assert "Secure" in session_cookie_header


def test_login_no_secure_cookie_over_plain_http(client_auth):
    """Tanpa X-Forwarded-Proto (koneksi plain HTTP biasa, mis. localhost dev)
    → Set-Cookie TIDAK punya flag Secure, konsisten perilaku sebelum perbaikan
    ini (bukan regresi untuk dev/topologi tanpa TLS)."""
    resp = client_auth.post(
        "/login",
        data={"token": "test-secret-token", "next": "/"},
        follow_redirects=False,
    )
    set_cookie_headers = resp.headers.get_list("set-cookie")
    session_cookie_header = next(
        h for h in set_cookie_headers if h.startswith("openclawn_session=")
    )
    assert "Secure" not in session_cookie_header


def test_rate_limit_blocks_after_quota_exhausted(client_no_auth):
    """Kuota RateLimiter habis → /chat/stream 429 SEBELUM mencapai handler (tak butuh LLM nyata)."""
    import web.main as web_main

    key = "testclient"  # tanpa auth aktif, tak ada cookie sesi → fallback client.host TestClient
    for _ in range(web_main._rate_limiter.max_requests):
        web_main._rate_limiter.allow(key)  # habiskan kuota langsung, tanpa memanggil endpoint

    resp = client_no_auth.post(
        "/chat/stream", data={"message": "hi", "role": "pm", "session_id": "s1"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert resp.headers["retry-after"] == "60"
