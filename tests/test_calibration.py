"""Tests untuk RoutingCalibrator (advisor murni) + CalibrationStore (loop tertutup)."""

import pytest

from core.calibration import (
    CalibrationStore,
    RoutingCalibrator,
    MIN_SAMPLE_FOR_SIGNAL,
    OFFSET_MAX,
    OFFSET_MIN,
    ROUTER_OFFSET_KEY,
)
from core.router import SmartRouter
from infra.config import AppConfig
from infra.database import DatabaseManager


def _row(label, total, rate, avg_cost=0.0):
    """Baris calibration_report dummy."""
    return {
        "complexity_label": label,
        "total": total,
        "corrections": int(total * rate / 100),
        "correction_rate": rate,
        "avg_cost": avg_cost,
    }


# ── Sampel minimum ────────────────────────────────────────────────────────────


def test_no_recommendation_below_min_sample():
    """Sampel < MIN_SAMPLE → tidak ada saran (hindari noise dari N kecil)."""
    cal = RoutingCalibrator()
    # rate tinggi tapi total kecil → harus diabaikan
    report = [_row("simple", total=3, rate=66.0)]
    assert cal.analyze(report) == []


def test_recommendation_at_min_sample_boundary():
    """Tepat di ambang sampel minimum → saran sudah valid."""
    cal = RoutingCalibrator()
    report = [_row("simple", total=MIN_SAMPLE_FOR_SIGNAL, rate=30.0)]
    recs = cal.analyze(report)
    assert len(recs) == 1


# ── Under-provisioned ─────────────────────────────────────────────────────────


def test_high_correction_rate_flagged_under_provisioned():
    """Correction rate tinggi + sampel cukup → under_provisioned."""
    cal = RoutingCalibrator()
    report = [_row("simple", total=50, rate=35.0)]
    recs = cal.analyze(report)
    assert len(recs) == 1
    assert recs[0].issue == "under_provisioned"
    assert recs[0].label == "simple"
    assert "moderate" in recs[0].suggestion  # menyarankan naik ke tier berikutnya


def test_top_tier_under_provisioned_suggests_prompt_not_routing():
    """Label tertinggi (critical) yang sering dikoreksi → saran bukan soal routing."""
    cal = RoutingCalibrator()
    report = [_row("critical", total=30, rate=40.0, avg_cost=0.003)]
    recs = cal.analyze(report)
    assert recs[0].issue == "under_provisioned"
    assert "tertinggi" in recs[0].suggestion


# ── Over-provisioned ──────────────────────────────────────────────────────────


def test_cloud_label_low_correction_flagged_over_provisioned():
    """Label cloud berbiaya + correction rate rendah → over_provisioned (buang biaya)."""
    cal = RoutingCalibrator()
    report = [_row("complex", total=40, rate=2.0, avg_cost=0.001)]
    recs = cal.analyze(report)
    assert len(recs) == 1
    assert recs[0].issue == "over_provisioned"
    assert "moderate" in recs[0].suggestion  # turun ke tier lebih murah


def test_local_label_low_correction_not_flagged():
    """Label lokal (gratis) dengan correction rendah TIDAK di-flag — tidak ada biaya dihemat."""
    cal = RoutingCalibrator()
    report = [_row("simple", total=40, rate=2.0, avg_cost=0.0)]
    assert cal.analyze(report) == []


def test_cloud_label_zero_cost_not_flagged_over():
    """Label cloud tapi avg_cost=0 (anomali) tidak di-flag over-provisioned."""
    cal = RoutingCalibrator()
    report = [_row("complex", total=40, rate=2.0, avg_cost=0.0)]
    assert cal.analyze(report) == []


# ── Zona normal ───────────────────────────────────────────────────────────────


def test_moderate_correction_rate_no_recommendation():
    """Correction rate di zona normal (antara low dan high) → tidak ada saran."""
    cal = RoutingCalibrator()
    report = [_row("moderate", total=50, rate=12.0)]
    assert cal.analyze(report) == []


# ── summary() ─────────────────────────────────────────────────────────────────


def test_summary_empty_report():
    """Report kosong → summary tidak crash, has_enough_data=False."""
    cal = RoutingCalibrator()
    s = cal.summary([])
    assert s["total_events"] == 0
    assert s["has_enough_data"] is False
    assert s["recommendations"] == []


def test_summary_aggregates_events_and_recs():
    """summary harus menjumlah total events dan menyertakan rekomendasi."""
    cal = RoutingCalibrator()
    report = [
        _row("simple", total=50, rate=35.0),  # under
        _row("complex", total=30, rate=2.0, avg_cost=0.001),  # over
        _row("moderate", total=20, rate=10.0),  # normal
    ]
    s = cal.summary(report)
    assert s["total_events"] == 100
    assert s["has_enough_data"] is True
    assert len(s["recommendations"]) == 2
    issues = {r["issue"] for r in s["recommendations"]}
    assert issues == {"under_provisioned", "over_provisioned"}


def test_summary_insufficient_total_data():
    """Total event sedikit → has_enough_data=False meski ada baris."""
    cal = RoutingCalibrator()
    report = [_row("simple", total=3, rate=50.0)]
    s = cal.summary(report)
    assert s["has_enough_data"] is False
    assert s["recommendations"] == []  # juga tak ada saran karena di bawah min sample


# ── Custom threshold ──────────────────────────────────────────────────────────


def test_custom_thresholds_respected():
    """Threshold yang diinjeksi via konstruktor harus dipakai."""
    cal = RoutingCalibrator(min_sample=5, high_rate=50.0)
    # rate 35 < high_rate 50 → tidak under-provisioned dengan threshold custom
    assert cal.analyze([_row("simple", total=10, rate=35.0)]) == []
    # rate 60 >= 50 → ter-flag
    recs = cal.analyze([_row("simple", total=10, rate=60.0)])
    assert len(recs) == 1


# ── offset_delta arah saran ───────────────────────────────────────────────────


def test_under_provisioned_suggests_negative_offset():
    """Under-provisioned → offset_delta -1 (router naik tier lebih cepat)."""
    cal = RoutingCalibrator()
    recs = cal.analyze([_row("simple", total=50, rate=35.0)])
    assert recs[0].offset_delta == -1


def test_over_provisioned_suggests_positive_offset():
    """Over-provisioned → offset_delta +1 (bertahan tier murah lebih lama)."""
    cal = RoutingCalibrator()
    recs = cal.analyze([_row("complex", total=40, rate=2.0, avg_cost=0.001)])
    assert recs[0].offset_delta == +1


def test_summary_net_offset_clamped_to_one_step():
    """net_offset_delta dijepit ke satu langkah meski banyak saran searah."""
    cal = RoutingCalibrator()
    report = [
        _row("simple", total=50, rate=35.0),  # under → -1
        _row("moderate", total=50, rate=40.0),  # under → -1
    ]
    s = cal.summary(report)
    assert s["net_offset_delta"] == -1  # bukan -2


def test_summary_net_offset_zero_when_conflicting():
    """Saran berlawanan arah (under + over) → net 0 (tak ada arah tunggal)."""
    cal = RoutingCalibrator()
    report = [
        _row("simple", total=50, rate=35.0),  # under → -1
        _row("complex", total=50, rate=2.0, avg_cost=0.001),  # over → +1
    ]
    s = cal.summary(report)
    assert s["net_offset_delta"] == 0


# ── CalibrationStore: loop tertutup (DB-bound) ────────────────────────────────


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_offset_defaults_to_zero(db):
    """Belum pernah diset → offset 0 (router asli)."""
    store = CalibrationStore(db)
    assert await store.get_offset() == 0


@pytest.mark.asyncio
async def test_apply_shifts_offset_and_logs_audit(db):
    """apply menggeser offset, menulis app_settings, dan mencatat baris audit aktif."""
    store = CalibrationStore(db)
    result = await store.apply(-1, reason="simple/under_provisioned")
    assert result == {"old_offset": 0, "new_offset": -1, "changed": True}
    assert await store.get_offset() == -1

    # app_settings terisi
    row = await db.fetchone("SELECT value FROM app_settings WHERE key=?", (ROUTER_OFFSET_KEY,))
    assert row["value"] == "-1"

    # tepat satu baris audit aktif
    active = await db.fetchall("SELECT * FROM calibration_log WHERE active=1")
    assert len(active) == 1
    assert active[0]["reason"] == "simple/under_provisioned"
    assert active[0]["source"] == "calibration"


@pytest.mark.asyncio
async def test_apply_clamped_to_bounds(db):
    """Offset tidak boleh melewati [OFFSET_MIN, OFFSET_MAX] meski di-apply berulang."""
    store = CalibrationStore(db)
    for _ in range(OFFSET_MAX + 5):
        await store.apply(+1, reason="paksa naik")
    assert await store.get_offset() == OFFSET_MAX

    for _ in range(OFFSET_MAX - OFFSET_MIN + 5):
        await store.apply(-1, reason="paksa turun")
    assert await store.get_offset() == OFFSET_MIN


@pytest.mark.asyncio
async def test_revert_restores_previous_offset(db):
    """revert mengembalikan offset ke state sebelum apply aktif terakhir."""
    store = CalibrationStore(db)
    await store.apply(-1, reason="apply pertama")  # 0 → -1
    result = await store.revert()
    assert result["reverted"] is True
    assert result["new_offset"] == 0
    assert await store.get_offset() == 0

    # hanya satu baris aktif (baris revert), audit lama tetap tersimpan
    active = await db.fetchall("SELECT * FROM calibration_log WHERE active=1")
    assert len(active) == 1
    assert active[0]["source"] == "revert"


@pytest.mark.asyncio
async def test_revert_noop_when_no_history(db):
    """revert tanpa riwayat → no-op, offset tetap 0, tidak crash."""
    store = CalibrationStore(db)
    result = await store.revert()
    assert result["reverted"] is False
    assert await store.get_offset() == 0


@pytest.mark.asyncio
async def test_only_one_active_row_after_multiple_applies(db):
    """Setiap apply menonaktifkan baris aktif sebelumnya — invarian audit."""
    store = CalibrationStore(db)
    await store.apply(-1, reason="a")
    await store.apply(-1, reason="b")
    await store.apply(+1, reason="c")
    active = await db.fetchall("SELECT * FROM calibration_log WHERE active=1")
    assert len(active) == 1
    assert active[0]["reason"] == "c"


@pytest.mark.asyncio
async def test_corrupt_offset_value_fails_safe_to_zero(db):
    """Nilai offset korup di app_settings → get_offset fail-safe ke 0, tak crash router."""
    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)", (ROUTER_OFFSET_KEY, "bukan-angka")
    )
    store = CalibrationStore(db)
    assert await store.get_offset() == 0


# ── Router menghormati offset (loop tertutup terhubung) ───────────────────────


def test_router_negative_offset_upgrades_sooner():
    """Offset negatif → query yang sama mendarat di tier LEBIH tinggi (naik lebih cepat)."""
    base = SmartRouter(role="pm", threshold_offset=0)
    shifted = SmartRouter(role="pm", threshold_offset=-1)
    msgs = [{"role": "user", "content": "x"}]
    query = "tolong review arsitektur dan implementasi modul ini"  # tech kw → skor menengah
    base_label = base.decide(msgs, query).complexity
    shifted_label = shifted.decide(msgs, query).complexity
    order = ["trivial", "simple", "moderate", "complex", "critical"]
    assert order.index(shifted_label.value) >= order.index(base_label.value)


def test_router_positive_offset_stays_cheaper():
    """Offset positif → query yang sama mendarat di tier SAMA atau LEBIH rendah."""
    base = SmartRouter(role="pm", threshold_offset=0)
    shifted = SmartRouter(role="pm", threshold_offset=+1)
    msgs = [{"role": "user", "content": "x"}]
    query = "tolong review arsitektur dan implementasi modul ini"
    base_label = base.decide(msgs, query).complexity
    shifted_label = shifted.decide(msgs, query).complexity
    order = ["trivial", "simple", "moderate", "complex", "critical"]
    assert order.index(shifted_label.value) <= order.index(base_label.value)
