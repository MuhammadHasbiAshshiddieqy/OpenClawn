"""Test end-to-end untuk alur login OIDC di web/main.py (TODO.md § Prioritas 5).

Berbeda dari test_auth_web.py (shared-secret): di sini OIDC dikonfigurasi
SENDIRIAN (tanpa OPENCLAWN_AUTH_TOKEN) untuk membuktikan mode OIDC-only benar-
benar menegakkan auth (bukan celah fail-open) dan sesi tetap tervalidasi lewat
CONFIG.session_secret walau auth_token kosong. Network ke provider OIDC sungguhan
di-mock — test tak boleh memanggil provider nyata.
"""

import time
import warnings
from unittest.mock import patch

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

warnings.filterwarnings("ignore", category=DeprecationWarning)

ISSUER = "https://accounts.example.com"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
_TEST_KEY = RSAKey.generate_key(2048, private=True)


def _make_client_oidc(tmp_path, monkeypatch, oidc_configured: bool = True):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("OPENCLAWN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAWN_IDLE_TIMEOUT_SEC", raising=False)
    if oidc_configured:
        monkeypatch.setenv("OPENCLAWN_OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("OPENCLAWN_OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("OPENCLAWN_OIDC_CLIENT_SECRET", CLIENT_SECRET)
        monkeypatch.setenv("OPENCLAWN_OIDC_REDIRECT_BASE", "https://myapp.example.com")
        monkeypatch.setenv("OPENCLAWN_SESSION_SECRET", "test-session-secret")
    else:
        monkeypatch.delenv("OPENCLAWN_OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OPENCLAWN_OIDC_CLIENT_ID", raising=False)
        monkeypatch.delenv("OPENCLAWN_OIDC_CLIENT_SECRET", raising=False)

    import importlib

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    return TestClient(web_main.app)


@pytest.fixture
def client_oidc(tmp_path, monkeypatch):
    """OIDC dikonfigurasi SENDIRIAN — auth_token kosong (login hanya via provider)."""
    with _make_client_oidc(tmp_path, monkeypatch, oidc_configured=True) as c:
        yield c


@pytest.fixture
def client_no_oidc(tmp_path, monkeypatch):
    """Baseline: tak ada auth mode aktif sama sekali."""
    with _make_client_oidc(tmp_path, monkeypatch, oidc_configured=False) as c:
        yield c


def _make_discovery_doc():
    return {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def _make_jwks_dict():
    public_key = RSAKey.import_key(_TEST_KEY.as_dict(private=False))
    return {"keys": [public_key.as_dict(kid="test-key-1")]}


def _make_id_token(claims_override: dict | None = None) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "email": "user@example.com",
        "name": "Test User",
        "exp": int(time.time()) + 3600,
        "nonce": "will-be-overridden",
    }
    if claims_override:
        claims.update(claims_override)
    signing_key = RSAKey.import_key(
        _TEST_KEY.as_dict(private=True), parameters={"kid": "test-key-1"}
    )
    return jwt.encode(header, claims, signing_key)


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


def _fake_async_client(get_map, post_map=None):
    """Client httpx.AsyncClient palsu — routing berdasarkan suffix URL."""
    from unittest.mock import MagicMock

    client_obj = MagicMock()

    async def _get(url, **kwargs):
        for suffix, data in get_map.items():
            if url.endswith(suffix):
                return _FakeResponse(data)
        raise AssertionError(f"unexpected GET {url}")

    async def _post(url, **kwargs):
        for suffix, data in (post_map or {}).items():
            if url.endswith(suffix):
                return _FakeResponse(data)
        raise AssertionError(f"unexpected POST {url}")

    client_obj.get = _get
    client_obj.post = _post

    ctx = MagicMock()

    async def _aenter(_self):
        return client_obj

    async def _aexit(_self, *args):
        return None

    ctx.__aenter__ = _aenter
    ctx.__aexit__ = _aexit
    return MagicMock(return_value=ctx)


# ── Baseline: tanpa OIDC configured ──────────────────────────────────────────


def test_no_oidc_configured_login_oidc_redirects_to_login(client_no_oidc):
    resp = client_no_oidc.get("/login/oidc", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_no_oidc_health_reports_oidc_disabled(client_no_oidc):
    assert client_no_oidc.get("/health").json()["oidc_enabled"] is False


# ── OIDC-only: mode auth aktif TANPA shared-secret ───────────────────────────


def test_oidc_only_root_redirects_unauthenticated_to_login(client_oidc):
    """CRITICAL: OIDC-only harus MENEGAKKAN auth, bukan fail-open karena auth_token kosong."""
    resp = client_oidc.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_oidc_only_health_reports_auth_and_oidc_enabled(client_oidc):
    body = client_oidc.get("/health").json()
    assert body["auth_enabled"] is True
    assert body["oidc_enabled"] is True


def test_login_page_shows_oidc_button_when_configured(client_oidc):
    resp = client_oidc.get("/login")
    assert resp.status_code == 200
    assert "/login/oidc" in resp.text


def test_login_oidc_start_redirects_to_provider_with_state_and_nonce(client_oidc):
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc()}
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        resp = client_oidc.get("/login/oidc", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"{ISSUER}/authorize?")
    assert "state=" in resp.headers["location"]
    assert "nonce=" in resp.headers["location"]
    assert "openclawn_oidc_state" in resp.cookies
    assert "openclawn_oidc_nonce" in resp.cookies


def test_callback_full_flow_grants_session_and_access(client_oidc):
    """Alur penuh: start → provider (mock) → callback → sesi valid → akses granted."""
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc()}
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        start_resp = client_oidc.get("/login/oidc", follow_redirects=False)
    # Cookie jar httpx/TestClient membungkus nilai berisi ':' dengan tanda kutip
    # (RFC 6265 quoted-string) — strip sebelum split, murni artefak test client,
    # browser sungguhan mengirim nilai apa adanya ke server.
    state_cookie = start_resp.cookies["openclawn_oidc_state"].strip('"')
    nonce_cookie = start_resp.cookies["openclawn_oidc_nonce"].strip('"')
    state = state_cookie.split(":", 1)[0]

    id_token = _make_id_token({"nonce": nonce_cookie})
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc(), "jwks": _make_jwks_dict()},
        post_map={"token": {"id_token": id_token, "access_token": "at-1"}},
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        callback_resp = client_oidc.get(
            f"/auth/callback?code=abc123&state={state}", follow_redirects=False
        )

    assert callback_resp.status_code == 303
    assert callback_resp.headers["location"] == "/"
    assert "openclawn_session" in callback_resp.cookies

    # Sesi granted → akses ke halaman terproteksi kini berhasil.
    root_resp = client_oidc.get("/")
    assert root_resp.status_code == 200


def test_callback_state_mismatch_rejected(client_oidc):
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc()}
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        client_oidc.get("/login/oidc", follow_redirects=False)

    resp = client_oidc.get(
        "/auth/callback?code=abc123&state=wrong-state-value", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=true" in resp.headers["location"]
    assert "openclawn_session" not in resp.cookies


def test_callback_verification_failure_rejected(client_oidc):
    """ID token dengan nonce salah (mismatch replay-check) → login ditolak."""
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc()}
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        start_resp = client_oidc.get("/login/oidc", follow_redirects=False)
    state = start_resp.cookies["openclawn_oidc_state"].strip('"').split(":", 1)[0]

    id_token = _make_id_token({"nonce": "totally-wrong-nonce"})
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc(), "jwks": _make_jwks_dict()},
        post_map={"token": {"id_token": id_token, "access_token": "at-1"}},
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        resp = client_oidc.get(f"/auth/callback?code=abc123&state={state}", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=true" in resp.headers["location"]
    assert "openclawn_session" not in resp.cookies
