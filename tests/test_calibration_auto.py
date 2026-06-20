"""Test I4 — guarded auto-apply kalibrasi: opt-in, throttled, clamp ±1, revertible."""

import pytest

from core.calibration import AUTO_APPLY_TS_KEY, CalibrationStore
from infra.config import AppConfig
from infra.database import DatabaseManager


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


async def _seed_under_provisioned(db, n=30):
    """Banyak event 'simple' yang sering dikoreksi → rekomendasi upgrade (delta -1)."""
    for i in range(n):
        await db.execute(
            """INSERT INTO routing_events (session_id, role, query_text, complexity_label,
                   model_chosen, provider, had_correction, cost_usd)
               VALUES ('s','dev','q','simple','gemma4:e2b','ollama',?,0.0)""",
            (1 if i % 2 == 0 else 0,),  # 50% correction rate → di atas ambang
        )


async def test_disabled_by_default(db):
    cfg = AppConfig(db_path=":memory:")  # calibration_auto_apply=False default
    await _seed_under_provisioned(db)
    res = await CalibrationStore(db).maybe_auto_apply(cfg)
    assert res["applied"] is False and res["reason"] == "disabled"


async def test_auto_apply_shifts_offset(db):
    cfg = AppConfig(db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=10)
    await _seed_under_provisioned(db)
    store = CalibrationStore(db)
    res = await store.maybe_auto_apply(cfg)
    assert res["applied"] is True
    assert res["delta"] == -1  # under-provisioned → naik tier lebih cepat
    assert await store.get_offset() == -1
    # source='auto' tercatat.
    row = await db.fetchone("SELECT source FROM calibration_log WHERE active=1")
    assert row["source"] == "auto"


async def test_clamped_to_max_step(db):
    """Walau banyak rekomendasi searah, geser tak pernah > ±1."""
    cfg = AppConfig(db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=10)
    await _seed_under_provisioned(db, n=40)
    store = CalibrationStore(db)
    res = await store.maybe_auto_apply(cfg)
    assert abs(res["delta"]) <= 1


async def test_insufficient_data_skips(db):
    cfg = AppConfig(
        db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=100
    )
    await _seed_under_provisioned(db, n=10)
    res = await CalibrationStore(db).maybe_auto_apply(cfg)
    assert res["applied"] is False and res["reason"] == "insufficient_data"


async def test_throttled_on_second_call(db):
    cfg = AppConfig(db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=10)
    await _seed_under_provisioned(db)
    store = CalibrationStore(db)
    await store.maybe_auto_apply(cfg)
    # Panggilan kedua langsung → throttled (interval default 1 hari).
    res2 = await store.maybe_auto_apply(cfg)
    assert res2["applied"] is False and res2["reason"] == "throttled"


async def test_auto_apply_is_revertible(db):
    cfg = AppConfig(db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=10)
    await _seed_under_provisioned(db)
    store = CalibrationStore(db)
    await store.maybe_auto_apply(cfg)
    assert await store.get_offset() == -1
    await store.revert()
    assert await store.get_offset() == 0  # kembali ke semula


async def test_timestamp_recorded(db):
    cfg = AppConfig(db_path=":memory:", calibration_auto_apply=True, calibration_auto_min_sample=10)
    await _seed_under_provisioned(db)
    await CalibrationStore(db).maybe_auto_apply(cfg)
    row = await db.fetchone("SELECT value FROM app_settings WHERE key=?", (AUTO_APPLY_TS_KEY,))
    assert row is not None and float(row["value"]) > 0
