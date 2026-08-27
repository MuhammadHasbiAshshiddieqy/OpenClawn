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


# ── Audit produksi 2026-07-29: 5 endpoint system-config yang sebelumnya tak
# digate sama sekali — role apa pun yang login bisa geser offset router,
# expose skill privat lintas-role, atau buat/toggle autopilot orang lain. ──


def test_member_forbidden_from_calibration_apply(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/calibration/apply",
        data={"csrf_token": csrf, "delta": "1", "reason": "uji"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_member_forbidden_from_calibration_revert(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/calibration/revert", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 403


def test_member_forbidden_from_audit_verify(client_oidc):
    """§ Prioritas 9.1: verifikasi rantai audit mengungkap ada/tidaknya
    manipulasi riwayat — informasi sensitif, admin-only."""
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/audit/verify")
    assert resp.status_code == 403


def test_member_forbidden_from_audit_anchor(client_oidc):
    """§ Prioritas 9.1 follow-up: menulis anchor adalah config sistem sensitif
    (mengubah file di luar database), admin-only."""
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post("/audit/anchor", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 403


def test_member_forbidden_from_skills_set_visibility(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/skills/set-visibility",
        data={"csrf_token": csrf, "skill_id": "1", "visibility": "shared"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_member_forbidden_from_autopilots_create(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/autopilots",
        data={
            "csrf_token": csrf,
            "name": "x",
            "role": "pm",
            "prompt": "y",
            "every": "1",
            "unit": "hour",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_member_forbidden_from_autopilots_toggle(client_oidc):
    import asyncio

    asyncio.run(_bootstrap_first_user_directly())
    _login_via_oidc(client_oidc, "user-bob")
    csrf = client_oidc.cookies.get("openclawn_csrf")
    resp = client_oidc.post(
        "/autopilots/toggle",
        data={"csrf_token": csrf, "autopilot_id": "1", "enabled": "1"},
        follow_redirects=False,
    )
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


# ── Audit produksi 2026-07-29: chat_sessions & approval sebelumnya tak punya
# isolasi per-user (hanya per-tenant) — user manapun yang login bisa baca/hapus
# chat user lain, atau lihat/putuskan approval milik user lain. ──────────────


async def _bootstrap_admin_and_get_id() -> int:
    """Sama pola _bootstrap_first_user_directly, tapi mengembalikan id user
    admin yang dibuat (dipakai untuk seed data owner_user_id di test ownership)."""
    from infra.database import DatabaseManager
    from infra.config import CONFIG
    from infra.users import UserStore

    db = DatabaseManager(CONFIG)
    await db.run_migration("migrations/001_initial.sql")
    user = await UserStore(db).upsert_on_login("user-alice-admin")
    await db.close()
    return user.id


def test_member_cannot_see_other_users_chat_session_in_list(client_oidc):
    """GET /chat-sessions HANYA menampilkan sesi milik user ini (+ tanpa owner)."""
    import asyncio
    from infra.chat_sessions import ChatSessionStore

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())

    import web.main as web_main

    asyncio.run(
        ChatSessionStore(web_main.db).ensure_created(
            "s-alice-private", "pm", owner_user_id=str(alice_id)
        )
    )

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/chat-sessions")
    session_ids = [s["session_id"] for s in resp.json()["sessions"]]
    assert "s-alice-private" not in session_ids


def test_member_forbidden_from_reading_other_users_chat_turns(client_oidc):
    import asyncio
    from infra.chat_sessions import ChatSessionStore

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())

    import web.main as web_main

    asyncio.run(
        ChatSessionStore(web_main.db).ensure_created(
            "s-alice-private", "pm", owner_user_id=str(alice_id)
        )
    )

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/chat-sessions/s-alice-private/turns")
    assert resp.status_code == 403


def test_member_forbidden_from_deleting_other_users_chat_session(client_oidc):
    import asyncio
    from infra.chat_sessions import ChatSessionStore

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())

    import web.main as web_main

    asyncio.run(
        ChatSessionStore(web_main.db).ensure_created(
            "s-alice-private", "pm", owner_user_id=str(alice_id)
        )
    )

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.delete("/chat-sessions/s-alice-private")
    assert resp.status_code == 403
    # Sesi tetap ada — tak terhapus oleh percobaan yang ditolak.
    row = asyncio.run(
        web_main.db.fetchone(
            "SELECT deleted_at FROM chat_sessions WHERE session_id='s-alice-private'"
        )
    )
    assert row["deleted_at"] is None


def test_member_can_still_access_own_chat_session(client_oidc):
    """Isolasi per-user tak berarti user tak bisa akses sesi MILIKNYA SENDIRI.

    Seed langsung via ChatSessionStore (bukan lewat /chat/stream sungguhan) —
    CLAUDE.md §5 test tak boleh memanggil LLM sungguhan; endpoint itu akan
    coba hubungi Ollama/Claude/Gemini asli lewat TestClient tanpa mock."""
    import asyncio
    from infra.chat_sessions import ChatSessionStore
    from infra.users import UserStore

    asyncio.run(_bootstrap_admin_and_get_id())
    _login_via_oidc(client_oidc, "user-bob")

    import web.main as web_main

    async def _seed_bob_session():
        bob = await UserStore(web_main.db).get_by_subject("user-bob")
        await ChatSessionStore(web_main.db).ensure_created(
            "s-bob-own", "pm", owner_user_id=str(bob.id)
        )

    asyncio.run(_seed_bob_session())

    resp = client_oidc.get("/chat-sessions/s-bob-own/turns")
    assert resp.status_code == 200


def _seed_pending_approval(owner_user_id: str | None, tool_input: dict) -> str:
    """Masukkan PendingApproval langsung ke `approval_gate._pending` GLOBAL milik
    app — TIDAK lewat `request()` (yang blocking sampai di-resolve/timeout).
    TestClient (starlette) menjalankan tiap call di portal/event-loop terpisah
    dari test sync ini, jadi Task/coroutine tak bisa dibagi lintas panggilan —
    manipulasi dict langsung menghindari itu sekaligus lebih sederhana untuk
    menguji logika filter/gate murni (bukan alur blocking request() itu sendiri,
    yang sudah diuji terpisah di tests/test_security.py).

    Dibungkus asyncio.run() semata agar `PendingApproval.future` (default_factory
    butuh running loop, Python 3.12 tak lagi auto-create) berhasil dibuat — Future
    itu sendiri tak pernah di-await lintas test ini, hanya `resolve()`/`.done()`
    yang disentuh (aman dipanggil dari loop lain selama tak ada callback
    terdaftar padanya, yang memang tak pernah terjadi di sini)."""
    import asyncio

    import web.main as web_main
    from security.approval import PendingApproval

    approval_id = f"test-{owner_user_id}-{len(web_main.approval_gate._pending)}"

    async def _build() -> PendingApproval:
        return PendingApproval(
            approval_id=approval_id,
            session_id="s-test",
            tool_name="code_run",
            tool_input=tool_input,
            owner_user_id=owner_user_id,
        )

    web_main.approval_gate._pending[approval_id] = asyncio.run(_build())
    return approval_id


def test_member_cannot_see_other_users_pending_approval(client_oidc):
    """GET /approvals tanpa session_id HANYA menampilkan approval milik user ini
    — SEBELUMNYA bocor approval SEMUA user (hijack lintas-user)."""
    import asyncio

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())
    _seed_pending_approval(str(alice_id), {"code": "rm -rf /"})

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get("/approvals")
    tool_inputs = [p["tool_input"] for p in resp.json()["pending"]]
    assert {"code": "rm -rf /"} not in tool_inputs


def test_member_forbidden_from_approving_other_users_approval(client_oidc):
    """POST /approve dengan approval_id milik user lain harus ditolak 403 —
    SEBELUMNYA approval_id APA PUN bisa diputuskan siapa pun (approval hijack)."""
    import asyncio

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())
    approval_id = _seed_pending_approval(str(alice_id), {"code": "x"})

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.post("/approve", data={"approval_id": approval_id, "decision": "approve"})
    assert resp.status_code == 403

    import web.main as web_main

    # Approval tetap pending — bob tak berhasil resolve.
    assert any(p["approval_id"] == approval_id for p in web_main.approval_gate.pending_list())


def test_admin_can_see_and_resolve_member_approval(client_oidc):
    """Admin (oversight) TETAP bisa lihat & putuskan approval milik user lain —
    isolasi hanya untuk role non-admin."""
    import asyncio

    asyncio.run(_bootstrap_admin_and_get_id())
    approval_id = _seed_pending_approval("999", {"code": "bob-action"})

    # Login sebagai alice (admin) — subject sama dengan yang dibootstrap.
    _login_via_oidc(client_oidc, "user-alice-admin")
    resp = client_oidc.get("/approvals")
    tool_inputs = [p["tool_input"] for p in resp.json()["pending"]]
    assert {"code": "bob-action"} in tool_inputs

    resp = client_oidc.post("/approve", data={"approval_id": approval_id, "decision": "approve"})
    assert resp.json()["ok"] is True


# ── Audit produksi 2026-07-30: GET /workspace/download tanpa cek kepemilikan
# saat session_id diberikan — user manapun bisa unduh file sesi orang lain. ──


def test_member_forbidden_from_downloading_other_users_session_file(client_oidc, tmp_path):
    import asyncio

    from infra.chat_sessions import ChatSessionStore

    alice_id = asyncio.run(_bootstrap_admin_and_get_id())

    import web.main as web_main

    (tmp_path / "alice-secret.txt").write_text("rahasia alice")
    asyncio.run(
        ChatSessionStore(web_main.db).ensure_created(
            "s-alice-file", "pm", owner_user_id=str(alice_id)
        )
    )

    _login_via_oidc(client_oidc, "user-bob")
    resp = client_oidc.get(
        "/workspace/download",
        params={"path": "alice-secret.txt", "session_id": "s-alice-file"},
    )
    assert resp.status_code == 403


def test_member_can_download_own_session_file(client_oidc, tmp_path):
    import asyncio

    from infra.chat_sessions import ChatSessionStore
    from infra.users import UserStore

    asyncio.run(_bootstrap_admin_and_get_id())
    _login_via_oidc(client_oidc, "user-bob")

    import web.main as web_main

    (tmp_path / "bob-file.txt").write_text("milik bob")

    async def _seed_bob_session():
        bob = await UserStore(web_main.db).get_by_subject("user-bob")
        await ChatSessionStore(web_main.db).ensure_created(
            "s-bob-file", "pm", owner_user_id=str(bob.id)
        )

    asyncio.run(_seed_bob_session())

    # workspace test client diarahkan ke tmp_path (lihat OPENCLAWN_WORKSPACE di
    # client_oidc), jadi tanpa custom workdir sesi, fallback ke root itu — cukup
    # untuk membuktikan kepemilikan LOLOS (403 tak muncul), bukan salah folder.
    resp = client_oidc.get(
        "/workspace/download",
        params={"path": "bob-file.txt", "session_id": "s-bob-file"},
    )
    assert resp.status_code == 200
    assert resp.text == "milik bob"
