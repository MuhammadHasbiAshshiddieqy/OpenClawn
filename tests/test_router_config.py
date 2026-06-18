"""Test RouterConfigStore + SmartRouter.model_map override (pilih model tiap tier via UI).

DB :memory:. Router tetap memutuskan tier; store hanya menentukan model tier.
"""

import pytest

from core.router import Complexity, SmartRouter
from core.router_config import ROUTER_MODEL_MAP_KEY, RouterConfigStore
from infra.config import AppConfig
from infra.database import DatabaseManager


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await manager.conn()
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


# ── RouterConfigStore ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_map_when_unset(db):
    """Tanpa override → peta = MODELS default penuh."""
    store = RouterConfigStore(db)
    assert await store.get_map() == dict(SmartRouter.MODELS)
    assert await store.is_overridden() is False


@pytest.mark.asyncio
async def test_set_and_get_partial_override(db):
    """Override sebagian tier → tier itu berubah, sisanya tetap default."""
    store = RouterConfigStore(db)
    await store.set_map({"trivial": {"provider": "gemini", "model": "gemini-2.0-flash"}})
    m = await store.get_map()
    assert m[Complexity.TRIVIAL] == ("gemini-2.0-flash", "gemini", 0.0)
    # tier lain tetap default
    assert m[Complexity.CRITICAL] == SmartRouter.MODELS[Complexity.CRITICAL]
    assert await store.is_overridden() is True


@pytest.mark.asyncio
async def test_unknown_provider_rejected(db):
    """Provider tak dikenal tidak disimpan → tier tetap default."""
    store = RouterConfigStore(db)
    await store.set_map({"simple": {"provider": "bogus", "model": "x"}})
    m = await store.get_map()
    assert m[Complexity.SIMPLE] == SmartRouter.MODELS[Complexity.SIMPLE]


@pytest.mark.asyncio
async def test_reset_clears_override(db):
    """reset() menghapus override → kembali default."""
    store = RouterConfigStore(db)
    await store.set_map({"trivial": {"provider": "anthropic", "model": "claude-sonnet-4-6"}})
    await store.reset()
    assert await store.get_map() == dict(SmartRouter.MODELS)
    assert await store.is_overridden() is False


@pytest.mark.asyncio
async def test_corrupt_value_falls_safe_to_default(db):
    """Nilai JSON korup → fail-safe ke peta default penuh, tak crash."""
    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)", (ROUTER_MODEL_MAP_KEY, "{bukan json")
    )
    assert await RouterConfigStore(db).get_map() == dict(SmartRouter.MODELS)


@pytest.mark.asyncio
async def test_partial_entry_missing_model_ignored(db):
    """Entry tanpa model diabaikan → tier tetap default (tidak setengah jadi)."""
    store = RouterConfigStore(db)
    await store.set_map({"moderate": {"provider": "ollama"}})  # tanpa model
    m = await store.get_map()
    assert m[Complexity.MODERATE] == SmartRouter.MODELS[Complexity.MODERATE]


# ── SmartRouter menghormati model_map ─────────────────────────────────────────


def test_router_uses_overridden_model_for_tier():
    """decide() memakai model_map override, bukan MODELS, untuk tier terpilih."""
    r = SmartRouter(role="pm")
    # Override tier TRIVIAL ke gemini; query pendek → tier TRIVIAL.
    r.model_map = dict(SmartRouter.MODELS)
    r.model_map[Complexity.TRIVIAL] = ("gemini-2.0-flash", "gemini", 0.0)
    decision = r.decide([{"role": "user", "content": "hi"}], "hi")
    assert decision.complexity == Complexity.TRIVIAL
    assert decision.model == "gemini-2.0-flash"
    assert decision.provider == "gemini"


def test_router_falls_back_to_models_if_tier_missing():
    """model_map parsial (tier hilang) → fallback ke MODELS untuk tier itu."""
    r = SmartRouter(role="pm")
    r.model_map = {}  # kosong total
    decision = r.decide([{"role": "user", "content": "hi"}], "hi")
    # tetap dapat model default tier-nya, tidak KeyError
    assert decision.model == SmartRouter.MODELS[decision.complexity][0]
