"""Test SandboxReaper (§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 3,
sandbox lifecycle). Pola sama `tests/test_autopilot.py` — `run_due_once()`
dipanggil langsung dengan `now` terkontrol, tanpa menunggu tick nyata. Docker
di-mock seluruhnya via `sandbox=` yang di-inject (tidak ada container sungguhan)."""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from core.sandbox_reaper import SandboxReaper
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.sandbox_lifecycle import SessionSandboxContainerStore


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


def _fake_sandbox():
    sandbox = AsyncMock()
    sandbox.pause_persistent = AsyncMock(return_value={"ok": True, "error": None})
    sandbox.destroy_persistent = AsyncMock(return_value=None)
    return sandbox


async def _seed(db, session_id, container_id, volume_name, minutes_ago, state="running"):
    store = SessionSandboxContainerStore(db)
    await store.create(session_id, container_id, volume_name)
    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await db.execute(
        "UPDATE session_sandbox_container SET last_used_at=?, state=? WHERE session_id=?",
        (past, state, session_id),
    )


@pytest.mark.asyncio
async def test_idle_row_gets_paused(db):
    config = dataclasses.replace(
        AppConfig(db_path=":memory:"),
        sandbox_persist_idle_ttl_sec=600,
        sandbox_persist_destroy_ttl_sec=3600,
    )
    await _seed(db, "sess-idle", "cid-1", "vol-1", minutes_ago=15)  # > 10 min idle
    sandbox = _fake_sandbox()
    reaper = SandboxReaper(db, config=config, sandbox=sandbox)

    result = await reaper.run_due_once()

    assert result["paused"] == ["sess-idle"]
    assert result["destroyed"] == []
    sandbox.pause_persistent.assert_called_once_with("cid-1")
    row = await SessionSandboxContainerStore(db).get("sess-idle")
    assert row["state"] == "paused"


@pytest.mark.asyncio
async def test_long_idle_row_gets_destroyed_and_row_removed(db):
    config = dataclasses.replace(
        AppConfig(db_path=":memory:"),
        sandbox_persist_idle_ttl_sec=600,
        sandbox_persist_destroy_ttl_sec=3600,
    )
    await _seed(db, "sess-old", "cid-2", "vol-2", minutes_ago=120)  # > 60 min
    sandbox = _fake_sandbox()
    reaper = SandboxReaper(db, config=config, sandbox=sandbox)

    result = await reaper.run_due_once()

    assert result["destroyed"] == ["sess-old"]
    assert result["paused"] == []
    sandbox.destroy_persistent.assert_called_once_with("cid-2", "vol-2")
    assert await SessionSandboxContainerStore(db).get("sess-old") is None


@pytest.mark.asyncio
async def test_freshly_used_row_untouched(db):
    config = dataclasses.replace(
        AppConfig(db_path=":memory:"),
        sandbox_persist_idle_ttl_sec=600,
        sandbox_persist_destroy_ttl_sec=3600,
    )
    await _seed(db, "sess-fresh", "cid-3", "vol-3", minutes_ago=1)
    sandbox = _fake_sandbox()
    reaper = SandboxReaper(db, config=config, sandbox=sandbox)

    result = await reaper.run_due_once()

    assert result == {"paused": [], "destroyed": []}
    sandbox.pause_persistent.assert_not_called()
    sandbox.destroy_persistent.assert_not_called()
    assert await SessionSandboxContainerStore(db).get("sess-fresh") is not None


@pytest.mark.asyncio
async def test_already_paused_row_not_paused_again_but_still_destroyed_when_old(db):
    config = dataclasses.replace(
        AppConfig(db_path=":memory:"),
        sandbox_persist_idle_ttl_sec=600,
        sandbox_persist_destroy_ttl_sec=3600,
    )
    await _seed(db, "sess-paused", "cid-4", "vol-4", minutes_ago=15, state="paused")
    sandbox = _fake_sandbox()
    reaper = SandboxReaper(db, config=config, sandbox=sandbox)

    result = await reaper.run_due_once()
    assert result == {"paused": [], "destroyed": []}
    sandbox.pause_persistent.assert_not_called()

    # Sekarang lewati destroy TTL juga — harus di-destroy meski statenya "paused".
    await db.execute(
        "UPDATE session_sandbox_container SET last_used_at=? WHERE session_id=?",
        (
            (datetime.now(timezone.utc) - timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S"),
            "sess-paused",
        ),
    )
    result = await reaper.run_due_once()
    assert result["destroyed"] == ["sess-paused"]


@pytest.mark.asyncio
async def test_pause_failure_is_logged_and_row_not_updated(db):
    config = dataclasses.replace(
        AppConfig(db_path=":memory:"),
        sandbox_persist_idle_ttl_sec=600,
        sandbox_persist_destroy_ttl_sec=3600,
    )
    await _seed(db, "sess-fail", "cid-5", "vol-5", minutes_ago=15)
    sandbox = _fake_sandbox()
    sandbox.pause_persistent = AsyncMock(return_value={"ok": False, "error": "boom"})
    reaper = SandboxReaper(db, config=config, sandbox=sandbox)

    result = await reaper.run_due_once()

    assert result["paused"] == []
    row = await SessionSandboxContainerStore(db).get("sess-fail")
    assert row["state"] == "running"  # tak berubah karena pause gagal


@pytest.mark.asyncio
async def test_run_due_once_callable_directly_without_real_ticks(db):
    """Sama semangat test_autopilot.py: reaper testable tanpa start()/asyncio
    sleep — run_due_once() bisa dipanggil langsung berulang kali."""
    config = AppConfig(db_path=":memory:")
    reaper = SandboxReaper(db, config=config, sandbox=_fake_sandbox())
    assert await reaper.run_due_once() == {"paused": [], "destroyed": []}
    assert await reaper.run_due_once() == {"paused": [], "destroyed": []}
