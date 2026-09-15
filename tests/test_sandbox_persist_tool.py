"""Test SandboxPersistEnableTool (§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md
Fase 3, sandbox lifecycle). Docker di-mock seluruhnya — tak ada container
sungguhan dibuat di suite pytest. Pola sama `tests/test_sandbox_image.py`."""

import dataclasses

import pytest
from unittest.mock import AsyncMock

from infra.config import AppConfig, CONFIG
from infra.database import DatabaseManager
from infra.sandbox_lifecycle import SessionSandboxContainerStore
from tools.sandbox import SandboxUnavailable
from tools.sandbox_persist import SandboxPersistEnableTool


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


def test_requires_approval():
    assert SandboxPersistEnableTool.requires_approval is True


@pytest.mark.asyncio
async def test_missing_session_id_errors(db):
    tool = SandboxPersistEnableTool()
    result = await tool.execute({}, vault=None, db=db)
    assert "error" in result


@pytest.mark.asyncio
async def test_missing_db_errors():
    tool = SandboxPersistEnableTool()
    result = await tool.execute({"_session_id": "s1"}, vault=None, db=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_success_persists_row(db):
    tool = SandboxPersistEnableTool()
    tool.sandbox.create_persistent = AsyncMock(
        return_value={"ok": True, "container_id": "openclawn-persist-abc", "volume_name": "vol-abc"}
    )

    result = await tool.execute({"_session_id": "sess-1"}, vault=None, db=db)

    assert result["ok"] is True
    assert result["already_enabled"] is False
    row = await SessionSandboxContainerStore(db).get("sess-1")
    assert row["container_id"] == "openclawn-persist-abc"


@pytest.mark.asyncio
async def test_idempotent_second_call_is_noop(db):
    tool = SandboxPersistEnableTool()
    tool.sandbox.create_persistent = AsyncMock(
        return_value={"ok": True, "container_id": "openclawn-persist-abc", "volume_name": "vol-abc"}
    )

    first = await tool.execute({"_session_id": "sess-1"}, vault=None, db=db)
    second = await tool.execute({"_session_id": "sess-1"}, vault=None, db=db)

    assert first["already_enabled"] is False
    assert second["already_enabled"] is True
    assert second["container_id"] == "openclawn-persist-abc"
    tool.sandbox.create_persistent.assert_called_once()  # container kedua TIDAK dibuat


@pytest.mark.asyncio
async def test_cap_enforcement_returns_clean_error(db, monkeypatch):
    """Batas sandbox_persist_max_containers tercapai → error terkontrol, bukan
    crash — permukaan DoS baru yang tak ada di model ephemeral."""
    patched = dataclasses.replace(CONFIG, sandbox_persist_max_containers=1)
    monkeypatch.setattr("tools.sandbox_persist.CONFIG", patched)

    await SessionSandboxContainerStore(db).create("sess-existing", "cid-x", "vol-x")

    tool = SandboxPersistEnableTool()
    tool.sandbox.create_persistent = AsyncMock()

    result = await tool.execute({"_session_id": "sess-new"}, vault=None, db=db)

    assert "error" in result
    tool.sandbox.create_persistent.assert_not_called()


@pytest.mark.asyncio
async def test_docker_unavailable_returns_error_not_raise(db):
    tool = SandboxPersistEnableTool()
    tool.sandbox.create_persistent = AsyncMock(
        side_effect=SandboxUnavailable("Docker tidak tersedia")
    )

    result = await tool.execute({"_session_id": "sess-1"}, vault=None, db=db)
    assert "error" in result


@pytest.mark.asyncio
async def test_create_failure_returns_error_dict(db):
    tool = SandboxPersistEnableTool()
    tool.sandbox.create_persistent = AsyncMock(return_value={"ok": False, "error": "boom"})

    result = await tool.execute({"_session_id": "sess-1"}, vault=None, db=db)
    assert "error" in result
    assert await SessionSandboxContainerStore(db).get("sess-1") is None


# ── Role allow-list: hanya role dengan code_run ────────────────────────────────


@pytest.mark.parametrize("role", ["dev", "qa", "data"])
def test_roles_with_code_run_allow_sandbox_persist_enable(role):
    import tomllib
    from pathlib import Path

    soul = tomllib.loads(Path(f"roles/{role}/soul.toml").read_text())
    assert "sandbox_persist_enable" in soul["tools"]["allowed"]


@pytest.mark.parametrize("role", ["pm", "security"])
def test_roles_without_code_run_do_not_allow_sandbox_persist_enable(role):
    import tomllib
    from pathlib import Path

    soul = tomllib.loads(Path(f"roles/{role}/soul.toml").read_text())
    assert "sandbox_persist_enable" not in soul["tools"]["allowed"]
    assert "code_run" not in soul["tools"]["allowed"]
