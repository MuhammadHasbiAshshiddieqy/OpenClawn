"""Tests untuk Sprint 4: RoutingCalibrator — rekomendasi tuning dari data audit."""

from core.calibration import (
    RoutingCalibrator,
    MIN_SAMPLE_FOR_SIGNAL,
)


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
