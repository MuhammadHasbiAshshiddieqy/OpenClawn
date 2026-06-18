"""Tests untuk Inovasi 1: RoutingAuditor — log, finalize, correction, calibration."""

import pytest
from dataclasses import dataclass
from core.audit import RoutingAuditor
from core.router import RouteDecision, Complexity
from infra.config import AppConfig
from infra.database import DatabaseManager


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


@pytest.fixture
def auditor(db):
    return RoutingAuditor(db=db)


def _fake_route(complexity=Complexity.SIMPLE, score=2, soul_hit=False):
    """RouteDecision dummy untuk testing."""
    return RouteDecision(
        model="gemma4:e4b",
        provider="ollama",
        complexity=complexity,
        complexity_score=score,
        reason="test reason",
        cost_per_1k=0.0,
        dimensions={
            "query_tokens": 5,
            "has_tech_kw": 0,
            "needs_multistep": 0,
            "history_len": 2,
            "role": "pm",
            "has_urgency": 0,
            "needs_stream": 1,
            "is_continuation": 0,
            "soul_upgrade_hit": int(soul_hit),
        },
        soul_upgrade_hit=soul_hit,
    )


@dataclass
class _FakeTurn:
    tokens_in: int = 100
    tokens_out: int = 50
    cost_usd: float = 0.0001
    latency_ms: int = 500
    fallback_used: bool = False


# ── log_decision + finalize ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_and_finalize_roundtrip(auditor, db):
    """log_decision → finalize: event tersimpan lengkap di DB."""
    route = _fake_route()
    event_id = await auditor.log_decision("s1", "pm", "test query", route)
    assert event_id is not None
    assert event_id > 0

    await auditor.finalize(event_id, _FakeTurn(tokens_in=200, tokens_out=100))

    row = await db.fetchone("SELECT * FROM routing_events WHERE id=?", (event_id,))
    assert row["query_text"] == "test query"
    assert row["tokens_in"] == 200
    assert row["tokens_out"] == 100
    assert row["had_correction"] == 0


@pytest.mark.asyncio
async def test_fallback_used_logged(auditor, db):
    """fallback_used=True harus tersimpan di DB."""
    route = _fake_route()
    event_id = await auditor.log_decision("s2", "dev", "complex task", route)
    await auditor.finalize(event_id, _FakeTurn(fallback_used=True))

    row = await db.fetchone("SELECT fallback_used FROM routing_events WHERE id=?", (event_id,))
    assert row["fallback_used"] == 1


@pytest.mark.asyncio
async def test_fallback_not_used_defaults_zero(auditor, db):
    """Turn tanpa fallback_used harus default ke 0."""
    route = _fake_route()
    event_id = await auditor.log_decision("s3", "qa", "query", route)
    await auditor.finalize(event_id, _FakeTurn())

    row = await db.fetchone("SELECT fallback_used FROM routing_events WHERE id=?", (event_id,))
    assert row["fallback_used"] == 0


@pytest.mark.asyncio
async def test_soul_upgrade_hit_logged(auditor, db):
    """soul_upgrade_hit harus tercatat di kolom dim_soul_upgrade_hit."""
    route = _fake_route(soul_hit=True)
    event_id = await auditor.log_decision("s4", "pm", "bantu arsitektur", route)

    row = await db.fetchone(
        "SELECT dim_soul_upgrade_hit FROM routing_events WHERE id=?", (event_id,)
    )
    assert row["dim_soul_upgrade_hit"] == 1


# ── check_correction ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correction_detected(auditor, db):
    """Query dengan sinyal koreksi harus menandai turn sebelumnya."""
    route = _fake_route()
    event_id = await auditor.log_decision("s5", "pm", "first query", route)
    await auditor.finalize(event_id, _FakeTurn())

    await auditor.check_correction("salah, bukan itu maksudku", "s5")

    row = await db.fetchone(
        "SELECT had_correction, correction_detail FROM routing_events WHERE id=?",
        (event_id,),
    )
    assert row["had_correction"] == 1
    assert "salah" in row["correction_detail"]


@pytest.mark.asyncio
async def test_correction_detected_english(auditor, db):
    """Sinyal koreksi bahasa Inggris juga terdeteksi (core locale-neutral §1.5)."""
    route = _fake_route()
    event_id = await auditor.log_decision("s5en", "pm", "first query", route)
    await auditor.finalize(event_id, _FakeTurn())

    await auditor.check_correction("no, that's wrong, try again", "s5en")

    row = await db.fetchone("SELECT had_correction FROM routing_events WHERE id=?", (event_id,))
    assert row["had_correction"] == 1


@pytest.mark.asyncio
async def test_no_correction_on_normal_query(auditor, db):
    """Query normal tanpa sinyal koreksi tidak boleh memicu had_correction."""
    route = _fake_route()
    event_id = await auditor.log_decision("s6", "pm", "normal query", route)
    await auditor.finalize(event_id, _FakeTurn())

    await auditor.check_correction("lanjutkan pekerjaan", "s6")

    row = await db.fetchone("SELECT had_correction FROM routing_events WHERE id=?", (event_id,))
    assert row["had_correction"] == 0


@pytest.mark.asyncio
async def test_correction_targets_most_recent_event(auditor, db):
    """check_correction harus menandai event PALING TERAKHIR di session."""
    route = _fake_route()
    e1 = await auditor.log_decision("s7", "pm", "query 1", route)
    await auditor.finalize(e1, _FakeTurn())
    e2 = await auditor.log_decision("s7", "pm", "query 2", route)
    await auditor.finalize(e2, _FakeTurn())

    await auditor.check_correction("ulangi!", "s7")

    row1 = await db.fetchone("SELECT had_correction FROM routing_events WHERE id=?", (e1,))
    row2 = await db.fetchone("SELECT had_correction FROM routing_events WHERE id=?", (e2,))
    assert row1["had_correction"] == 0  # e1 tidak dikoreksi
    assert row2["had_correction"] == 1  # e2 yang dikoreksi (paling baru)


# ── calibration_report ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calibration_report_empty(auditor):
    """Tanpa data, calibration_report harus return list kosong (tidak crash)."""
    report = await auditor.calibration_report()
    assert isinstance(report, list)
    assert len(report) == 0


@pytest.mark.asyncio
async def test_calibration_report_with_data(auditor):
    """Calibration report harus mengelompokkan per complexity_label."""
    # Insert events: 3 simple + 2 complex
    simple_ids = []
    for i in range(3):
        route = _fake_route(Complexity.SIMPLE)
        eid = await auditor.log_decision("s_cal", "pm", f"q-simple-{i}", route)
        await auditor.finalize(eid, _FakeTurn())
        simple_ids.append(eid)
    for i in range(2):
        route = _fake_route(Complexity.COMPLEX)
        eid = await auditor.log_decision("s_cal", "pm", f"q-complex-{i}", route)
        await auditor.finalize(eid, _FakeTurn())

    # Koreksi event SIMPLE pertama (bukan yang paling baru)
    await auditor.check_correction("salah!", "s_cal")

    report = await auditor.calibration_report()
    assert len(report) >= 1  # minimal SIMPLE dan COMPLEX muncul

    simple_row = [r for r in report if r["complexity_label"] == "simple"]
    assert len(simple_row) == 1
    # Paling tidak satu event dikoreksi (yang paling baru = COMPLEX)
    total_corrections = sum(r["corrections"] for r in report)
    assert total_corrections >= 1


@pytest.mark.asyncio
async def test_all_correction_signals(auditor, db):
    """Semua sinyal koreksi yang didefinisikan harus berfungsi."""
    from core.audit import CORRECTION_SIGNALS

    for i, signal in enumerate(CORRECTION_SIGNALS):
        # Gunakan session_id unik per sinyal agar tidak bentrok
        sid = f"s_sig_{i}"
        route = _fake_route()
        eid = await auditor.log_decision(sid, "pm", f"query-{signal}", route)
        await auditor.finalize(eid, _FakeTurn())
        await auditor.check_correction(f"tolong {signal}", sid)

        row = await db.fetchone("SELECT had_correction FROM routing_events WHERE id=?", (eid,))
        assert row["had_correction"] == 1, f"Sinyal '{signal}' tidak terdeteksi!"
