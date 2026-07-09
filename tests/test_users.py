"""Test untuk infra/users.py — multi-user + RBAC per tenant (TODO.md § Prioritas 5,
revisi eksplisit CLAUDE.md §7).
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.users import ACCESS_ROLES, SHARED_SECRET_SUBJECT, UserStore, role_at_least


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


# ── role_at_least (hierarki viewer < member < admin) ─────────────────────────


def test_role_at_least_admin_satisfies_all():
    for minimum in ACCESS_ROLES:
        assert role_at_least("admin", minimum) is True


def test_role_at_least_viewer_fails_member_and_admin():
    assert role_at_least("viewer", "member") is False
    assert role_at_least("viewer", "admin") is False
    assert role_at_least("viewer", "viewer") is True


def test_role_at_least_member_satisfies_member_and_viewer_not_admin():
    assert role_at_least("member", "viewer") is True
    assert role_at_least("member", "member") is True
    assert role_at_least("member", "admin") is False


def test_role_at_least_unknown_role_fails_safe():
    assert role_at_least("superuser", "viewer") is False
    assert role_at_least("admin", "superuser") is False


# ── UserStore.upsert_on_login: bootstrap admin pertama ───────────────────────


@pytest.mark.asyncio
async def test_first_user_bootstrapped_as_admin(db):
    store = UserStore(db)
    user = await store.upsert_on_login("user-alice", email="alice@example.com")
    assert user.access_role == "admin"


@pytest.mark.asyncio
async def test_second_user_defaults_to_member(db):
    store = UserStore(db)
    await store.upsert_on_login("user-alice")
    second = await store.upsert_on_login("user-bob")
    assert second.access_role == "member"


@pytest.mark.asyncio
async def test_upsert_idempotent_does_not_reset_role(db):
    """Login berulang tak menimpa role yang sudah di-set admin secara eksplisit."""
    store = UserStore(db)
    user = await store.upsert_on_login("user-alice")
    await store.set_access_role(user.id, "viewer")

    relogin = await store.upsert_on_login("user-alice", email="new-email@example.com")
    assert relogin.access_role == "viewer"  # TIDAK direset ke admin/member
    assert relogin.email == "new-email@example.com"  # tapi profil di-refresh


@pytest.mark.asyncio
async def test_shared_secret_subject_constant_used_consistently(db):
    """Shared-secret login selalu memetakan ke subject tetap — bootstrap admin."""
    store = UserStore(db)
    user = await store.upsert_on_login(SHARED_SECRET_SUBJECT)
    assert user.access_role == "admin"
    assert user.subject == "shared-secret"


# ── set_access_role ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_access_role_updates_role(db):
    store = UserStore(db)
    user = await store.upsert_on_login("user-alice")
    ok = await store.set_access_role(user.id, "viewer")
    assert ok is True
    updated = await store.get_by_id(user.id)
    assert updated.access_role == "viewer"


@pytest.mark.asyncio
async def test_set_access_role_rejects_unknown_role(db):
    store = UserStore(db)
    user = await store.upsert_on_login("user-alice")
    ok = await store.set_access_role(user.id, "superuser")
    assert ok is False
    unchanged = await store.get_by_id(user.id)
    assert unchanged.access_role == "admin"  # tak berubah


@pytest.mark.asyncio
async def test_set_access_role_unknown_user_returns_false(db):
    store = UserStore(db)
    ok = await store.set_access_role(999, "admin")
    assert ok is False


# ── list_users, get_by_subject/get_by_id ─────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_returns_all_in_tenant(db):
    store = UserStore(db)
    await store.upsert_on_login("user-alice")
    await store.upsert_on_login("user-bob")
    users = await store.list_users()
    assert [u.subject for u in users] == ["user-alice", "user-bob"]


@pytest.mark.asyncio
async def test_get_by_subject_unknown_returns_none(db):
    store = UserStore(db)
    assert await store.get_by_subject("nobody") is None


@pytest.mark.asyncio
async def test_get_by_id_unknown_returns_none(db):
    store = UserStore(db)
    assert await store.get_by_id(999) is None


# ── Isolasi tenant ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_users_scoped_to_tenant(db):
    """Subject sama boleh ada di tenant berbeda (unique constraint per-tenant),
    dan masing-masing dapat bootstrap admin sendiri."""
    store_a = UserStore(db, tenant_id="tenant-a")
    store_b = UserStore(db, tenant_id="tenant-b")

    user_a = await store_a.upsert_on_login("shared-subject")
    user_b = await store_b.upsert_on_login("shared-subject")

    assert user_a.access_role == "admin"  # pertama di tenant-a
    assert user_b.access_role == "admin"  # pertama di tenant-b (independen)
    assert user_a.id != user_b.id

    users_a = await store_a.list_users()
    users_b = await store_b.list_users()
    assert len(users_a) == 1
    assert len(users_b) == 1
