"""Test I5 — dialectic user model (opsional, versioned, revertible, privacy-clearable)."""

from unittest.mock import AsyncMock

import pytest

from core.llm_client import LLMChunk
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.user_model import UserModel


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


def _stream(text):
    async def s(provider, model, messages, *a, **k):
        yield LLMChunk(type="text", text=text)

    return s


async def _add_fact(db, fact, role="dev"):
    await db.execute("INSERT INTO memory_l2 (role, fact, importance) VALUES (?,?,1)", (role, fact))


async def test_disabled_by_default(db):
    cfg = AppConfig(db_path=":memory:")  # user_model_enabled=False
    um = UserModel("dev", db, AsyncMock(), cfg)
    await _add_fact(db, "user suka Python")
    res = await um.maybe_update()
    assert res["skipped"] and res["reason"] == "disabled"
    assert await um.get_active_profile() == ""


async def test_builds_profile_when_enabled(db):
    cfg = AppConfig(db_path=":memory:", user_model_enabled=True)
    llm = AsyncMock()
    llm.stream_with_fallback = _stream("User adalah engineer Python yang menyukai kode ringkas.")
    um = UserModel("dev", db, llm, cfg)
    await _add_fact(db, "user suka Python")
    res = await um.maybe_update()
    assert res["skipped"] is False and res["version"] == 1
    assert "Python" in await um.get_active_profile()


async def test_versioned_on_second_update(db):
    cfg = AppConfig(db_path=":memory:", user_model_enabled=True, user_model_interval_sec=0)
    llm = AsyncMock()
    llm.stream_with_fallback = _stream("profil v1")
    um = UserModel("dev", db, llm, cfg)
    await _add_fact(db, "fakta")
    await um.maybe_update()
    llm.stream_with_fallback = _stream("profil v2")
    res2 = await um.maybe_update()
    assert res2["version"] == 2
    # Hanya satu versi aktif.
    rows = await db.fetchall("SELECT version FROM user_model WHERE role='dev' AND active=1")
    assert len(rows) == 1 and rows[0]["version"] == 2


async def test_throttled(db):
    cfg = AppConfig(db_path=":memory:", user_model_enabled=True)  # interval default 1 hari
    llm = AsyncMock()
    llm.stream_with_fallback = _stream("profil")
    um = UserModel("dev", db, llm, cfg)
    await _add_fact(db, "fakta")
    await um.maybe_update()
    res2 = await um.maybe_update()
    assert res2["skipped"] and res2["reason"] == "throttled"


async def test_no_facts_skips(db):
    cfg = AppConfig(db_path=":memory:", user_model_enabled=True)
    um = UserModel("dev", db, AsyncMock(), cfg)
    res = await um.maybe_update()
    assert res["skipped"] and res["reason"] == "no_facts"


async def test_clear_removes_profile(db):
    cfg = AppConfig(db_path=":memory:", user_model_enabled=True)
    llm = AsyncMock()
    llm.stream_with_fallback = _stream("profil")
    um = UserModel("dev", db, llm, cfg)
    await _add_fact(db, "fakta")
    await um.maybe_update()
    await um.clear()
    assert await um.get_active_profile() == ""
