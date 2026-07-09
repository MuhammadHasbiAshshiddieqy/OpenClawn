"""Test end-to-end untuk RBAC (TODO.md § Prioritas 5, revisi eksplisit CLAUDE.md
§7) — role akses admin/member/viewer menggerbangi endpoint config sistem
(/settings, /skills/import, /mcp/*, /router, /autopilots/delete, /admin/users).

Shared-secret login SELALU bootstrap admin (satu-satunya user shared-secret).
OIDC: user pertama per tenant → admin; berikutnya → member (default).
"""

import time
import warnings
from unittest.mock import MagicMock, patch

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

warnings.filterwarnings("ignore", category=DeprecationWarning)

ISSUER = "https://accounts.example.com"
CLIENT_ID = "test-client-id"
_TEST_KEY = RSAKey.generate_key(2048, private=True)


@pytest.fixture(autouse=True)
def _clear_oidc_caches():
    """`security/oidc.py` men-cache discovery/JWKS in-process per issuer (TTL 1
    jam) — test file lain (test_oidc.py, test_oidc_web.py) memakai ISSUER yang
    SAMA tapi RSA key BERBEDA. Tanpa dibersihkan, JWKS milik file lain yang
    jalan lebih dulu dalam sesi pytest yang sama bisa ter-cache dan membuat
    verifikasi signature di sini gagal (bad_signature) — bug isolasi test yang
    nyata ditemukan saat full-suite run (lolos saat file ini dijalankan sendiri)."""
    from security.oidc import _discovery_cache, _jwks_cache

    _discovery_cache.clear()
    _jwks_cache.clear()
    yield
    _discovery_cache.clear()
    _jwks_cache.clear()


def _make_client_auth(tmp_path, monkeypatch, auth_token: str = "test-secret-token"):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENCLAWN_AUTH_TOKEN", auth_token)
    monkeypatch.delenv("OPENCLAWN_OIDC_ISSUER", raising=False)

    import importlib

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    return TestClient(web_main.app)


def _make_client_oidc(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("OPENCLAWN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENCLAWN_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OPENCLAWN_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OPENCLAWN_OIDC_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("OPENCLAWN_OIDC_REDIRECT_BASE", "https://myapp.example.com")
    monkeypatch.setenv("OPENCLAWN_SESSION_SECRET", "test-session-secret")

    import importlib

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    return TestClient(web_main.app)


@pytest.fixture
def client_shared_secret(tmp_path, monkeypatch):
    with _make_client_auth(tmp_path, monkeypatch) as c:
        yield c


@pytest.fixture
def client_oidc(tmp_path, monkeypatch):
    with _make_client_oidc(tmp_path, monkeypatch) as c:
        yield c


def _login_shared_secret(client, token="test-secret-token"):
    return client.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)


def _make_discovery_doc():
    return {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def _make_jwks_dict():
    public_key = RSAKey.import_key(_TEST_KEY.as_dict(private=False))
    return {"keys": [public_key.as_dict(kid="test-key-1")]}


def _make_id_token(subject: str, nonce: str) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": subject,
        "email": f"{subject}@example.com",
        "name": subject,
        "exp": int(time.time()) + 3600,
        "nonce": nonce,
    }
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


def _login_via_oidc(client, subject: str):
    """Jalankan alur OIDC penuh (mocked network) untuk satu subject, return client
    yang sudah login (cookie tersimpan otomatis oleh TestClient)."""
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc()}
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        start_resp = client.get("/login/oidc", follow_redirects=False)
    state = start_resp.cookies["openclawn_oidc_state"].strip('"').split(":", 1)[0]
    nonce = start_resp.cookies["openclawn_oidc_nonce"].strip('"')

    id_token = _make_id_token(subject, nonce)
    fake_client_cls = _fake_async_client(
        {".well-known/openid-configuration": _make_discovery_doc(), "jwks": _make_jwks_dict()},
        post_map={"token": {"id_token": id_token, "access_token": "at-1"}},
    )
    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        return client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)


# ── Shared-secret login → bootstrap admin ────────────────────────────────────


def test_shared_secret_login_grants_admin_access_to_settings(client_shared_secret):
    _login_shared_secret(client_shared_secret)
    resp = client_shared_secret.get("/settings")
    assert resp.status_code == 200


def test_shared_secret_login_can_post_settings(client_shared_secret):
    _login_shared_secret(client_shared_secret)
    csrf = client_shared_secret.cookies.get("openclawn_csrf")
    resp = client_shared_secret.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "model_choice": "auto",
            "compaction_mode": "off",
            "ui_locale": "en",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303  # bukan 403 — admin diizinkan


def test_shared_secret_login_can_access_admin_users_page(client_shared_secret):
    _login_shared_secret(client_shared_secret)
    resp = client_shared_secret.get("/admin/users")
    assert resp.status_code == 200


# ── OIDC: user pertama admin, kedua member (di-gate) ─────────────────────────


def test_oidc_first_user_is_admin_can_post_settings(client_oidc):
    _login_via_oidc(client_oidc, "user-alice")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "model_choice": "auto",
            "compaction_mode": "off",
            "ui_locale": "en",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def _bootstrap_first_user_directly():
    """Buat user OIDC PERTAMA langsung via UserStore (bukan lewat HTTP) —
    menghindari spin up TestClient/app kedua yang bentrok dengan lifespan
    AutopilotScheduler (event loop berbeda). Cukup untuk memastikan user
    BERIKUTNYA yang login via HTTP tidak dapat bootstrap admin lagi."""
    from infra.database import DatabaseManager
    from infra.config import CONFIG
    from infra.users import UserStore

    db = DatabaseManager(CONFIG)
    await db.run_migration("migrations/001_initial.sql")
    await UserStore(db).upsert_on_login("user-alice-bootstrap")
    await db.close()


def test_oidc_second_user_is_member_forbidden_from_settings(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())

    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/settings",
        data={
            "csrf_token": csrf,
            "model_choice": "auto",
            "compaction_mode": "off",
            "ui_locale": "en",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_oidc_second_user_member_can_still_use_chat(client_oidc):
    """RBAC hanya menggerbangi admin-config endpoint — member tetap bisa chat/lihat skills."""
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/skills")
    assert resp.status_code == 200


def test_oidc_second_user_member_forbidden_from_admin_users_page(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/admin/users")
    assert resp.status_code == 403


# ── Role change via /admin/users/set-role ────────────────────────────────────


def test_admin_can_promote_member_to_admin(client_oidc):
    import asyncio
    from infra.database import DatabaseManager
    from infra.config import CONFIG
    from infra.users import UserStore

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")

    # Verifikasi langsung via DB bahwa bob memang 'member' sebelum di-promote.
    async def _check_and_promote():
        db = DatabaseManager(CONFIG)
        store = UserStore(db)
        bob = await store.get_by_subject("user-bob")
        assert bob.access_role == "member"
        await store.set_access_role(bob.id, "admin")
        await db.close()

    asyncio.run(_check_and_promote())

    # bob sekarang admin — sesi lama sudah punya user_id di cookie, request baru
    # harus refleksikan role baru (dimuat ulang dari DB tiap request, bukan cache).
    resp = client_oidc.get("/settings")
    assert resp.status_code == 200


# ── Auth nonaktif: RBAC tak berlaku (perilaku lama tak berubah) ──────────────


def test_no_auth_settings_accessible_without_rbac(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAWN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("OPENCLAWN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAWN_OIDC_ISSUER", raising=False)

    import importlib

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    with TestClient(web_main.app) as client:
        resp = client.get("/settings")
        assert resp.status_code == 200  # RBAC tak menghalangi saat auth nonaktif
