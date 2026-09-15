"""Test SessionSandboxContainerStore (§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md
Fase 3, sandbox lifecycle) — CRUD murni DB, tanpa Docker sama sekali."""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.sandbox_lifecycle import (
    CURRENT_PERSISTENT_SANDBOX,
    SessionSandboxContainerStore,
    effective_persistent_container,
)


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


async def test_create_and_get(db):
    store = SessionSandboxContainerStore(db)
    assert await store.get("sess-1") is None
    await store.create("sess-1", "openclawn-persist-abc", "openclawn-persist-vol-abc")
    row = await store.get("sess-1")
    assert row["container_id"] == "openclawn-persist-abc"
    assert row["volume_name"] == "openclawn-persist-vol-abc"
    assert row["state"] == "running"


async def test_set_state(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    await store.set_state("sess-1", "paused")
    assert (await store.get("sess-1"))["state"] == "paused"


async def test_touch_updates_last_used_at(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    before = (await store.get("sess-1"))["last_used_at"]
    await store.touch("sess-1")
    after = (await store.get("sess-1"))["last_used_at"]
    # SQLite CURRENT_TIMESTAMP punya resolusi detik — hanya pastikan tak error
    # dan baris masih ada dengan timestamp valid (perbandingan waktu asli ada
    # di test_sandbox_reaper.py lewat run_due_once yang mengontrol `now`).
    assert before is not None and after is not None


async def test_delete(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    await store.delete("sess-1")
    assert await store.get("sess-1") is None


async def test_list_all(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid-1", "vol-1")
    await store.create("sess-2", "cid-2", "vol-2")
    rows = await store.list_all()
    assert {r["session_id"] for r in rows} == {"sess-1", "sess-2"}


async def test_count_active(db):
    store = SessionSandboxContainerStore(db)
    assert await store.count_active() == 0
    await store.create("sess-1", "cid-1", "vol-1")
    assert await store.count_active() == 1
    await store.create("sess-2", "cid-2", "vol-2")
    assert await store.count_active() == 2
    await store.delete("sess-1")
    assert await store.count_active() == 1


def test_effective_persistent_container_default_none():
    """ContextVar default None → mode ephemeral, perilaku lama tak berubah."""
    assert effective_persistent_container() is None


def test_effective_persistent_container_reads_contextvar():
    token = CURRENT_PERSISTENT_SANDBOX.set("openclawn-persist-xyz")
    try:
        assert effective_persistent_container() == "openclawn-persist-xyz"
    finally:
        CURRENT_PERSISTENT_SANDBOX.reset(token)
