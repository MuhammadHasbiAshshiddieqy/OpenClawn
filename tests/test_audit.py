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
async def test_log_decision_defaults_actor_is_agent_true(auditor, db):
    """Audit log format actor_is_agent (TODO.md § Prioritas 2, pola GitHub
    control plane): semua baris routing_events adalah tindakan AGENT, bukan
    manusia langsung — actor_is_agent harus 1 secara default tanpa perlu
    diberi eksplisit tiap kali dipanggil."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_actor1", "pm", "q", route)

    row = await db.fetchone("SELECT actor_is_agent FROM routing_events WHERE id=?", (event_id,))
    assert row["actor_is_agent"] == 1


@pytest.mark.asyncio
async def test_log_decision_stores_user_id_when_given(auditor, db):
    """user_id opsional (default 'default', single-user §7) tersimpan agar
    query-able terpisah dari session_id — memudahkan integrasi SIEM eksternal
    yang mengharapkan actor/user eksplisit, bukan cuma session opaque."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_actor2", "pm", "q", route, user_id="alice")

    row = await db.fetchone("SELECT user_id FROM routing_events WHERE id=?", (event_id,))
    assert row["user_id"] == "alice"


@pytest.mark.asyncio
async def test_log_decision_user_id_defaults_to_default_string(auditor, db):
    """Tanpa user_id eksplisit → 'default' (selaras AgentConfig.user_id default,
    bukan NULL — konsisten dengan single-user design saat ini, CLAUDE.md §7)."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_actor3", "pm", "q", route)

    row = await db.fetchone("SELECT user_id FROM routing_events WHERE id=?", (event_id,))
    assert row["user_id"] == "default"


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
async def test_finalize_stores_evidence_json(auditor, db):
    """Evidence-Based Response (TODO.md § Prioritas 2): finalize(evidence=...)
    menyimpan snapshot policy/skill/guardrail sebagai JSON query-able."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_ev1", "pm", "buat pdf", route)
    evidence = {
        "policy": {"provider": "gemini", "model": "gemini-2.5-flash", "complexity": "simple"},
        "memory": ["prd-template-skill"],
        "guardrail": {"status": "clean", "detail": ""},
    }
    await auditor.finalize(event_id, _FakeTurn(), evidence=evidence)

    row = await db.fetchone("SELECT evidence_json FROM routing_events WHERE id=?", (event_id,))
    assert row["evidence_json"] is not None
    import json

    stored = json.loads(row["evidence_json"])
    assert stored == evidence


@pytest.mark.asyncio
async def test_finalize_without_evidence_leaves_null(auditor, db):
    """finalize() tanpa argumen evidence (default None) — kolom tetap NULL,
    bukan string 'null' atau dict kosong (bedakan 'belum ada data' dari 'ada
    tapi kosong')."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_ev2", "pm", "query biasa", route)
    await auditor.finalize(event_id, _FakeTurn())

    row = await db.fetchone("SELECT evidence_json FROM routing_events WHERE id=?", (event_id,))
    assert row["evidence_json"] is None


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


@pytest.mark.asyncio
async def test_correction_writes_to_audit_chain(auditor, db):
    """§ Prioritas 9.1 follow-up (a): koreksi user HARUS dirantai — bukan cuma
    sinyal kalibrasi, itu bukti tindakan agent bermasalah."""
    route = _fake_route()
    event_id = await auditor.log_decision("s_corr_chain", "pm", "q", route)
    await auditor.finalize(event_id, _FakeTurn())

    await auditor.check_correction("salah, coba lagi", "s_corr_chain")

    row = await db.fetchone(
        "SELECT entry_type, ref_id, payload_json FROM audit_chain "
        "WHERE entry_type='routing.corrected' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["ref_id"] == event_id
    assert "salah" in row["payload_json"]


@pytest.mark.asyncio
async def test_correction_with_no_prior_event_does_not_write_to_chain(auditor, db):
    """Sinyal koreksi tanpa event sebelumnya di sesi (mis. turn pertama) TIDAK
    boleh menulis entry rantai palsu — tak ada yang benar-benar dikoreksi."""
    await auditor.check_correction("salah, coba lagi", "s_corr_no_prior")

    row = await db.fetchone("SELECT id FROM audit_chain WHERE entry_type='routing.corrected'")
    assert row is None


@pytest.mark.asyncio
async def test_correction_return_value_unchanged_regardless_of_prior_event(auditor):
    """Kontrak return value TAK BERUBAH oleh penambahan chain-append: True
    murni berdasar sinyal teks, terpakai SkillFeedback untuk resolve_previous."""
    assert await auditor.check_correction("salah, coba lagi", "s_no_such_session") is True
    assert await auditor.check_correction("lanjutkan pekerjaan", "s_no_such_session") is False


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


# ── role_report (Runtime Evaluation Engine, TODO.md § Prioritas 2) ──────────


@pytest.mark.asyncio
async def test_role_report_empty(auditor):
    """Tanpa data, role_report harus return list kosong (tidak crash)."""
    report = await auditor.role_report()
    assert isinstance(report, list)
    assert len(report) == 0


@pytest.mark.asyncio
async def test_role_report_groups_by_role(auditor):
    """role_report mengelompokkan per role (bukan per complexity_label seperti
    calibration_report) — KPI dashboard per-agent yang buyer enterprise cari."""
    for i in range(3):
        route = _fake_route()
        eid = await auditor.log_decision("s_role", "pm", f"q-{i}", route)
        await auditor.finalize(eid, _FakeTurn(cost_usd=0.001, latency_ms=200))
    for i in range(2):
        route = _fake_route()
        eid = await auditor.log_decision("s_role2", "dev", f"q-{i}", route)
        await auditor.finalize(eid, _FakeTurn(cost_usd=0.002, latency_ms=300))

    report = await auditor.role_report()
    by_role = {r["role"]: r for r in report}
    assert by_role["pm"]["total"] == 3
    assert by_role["dev"]["total"] == 2
    assert by_role["pm"]["avg_latency_ms"] == 200
    assert by_role["dev"]["avg_latency_ms"] == 300


@pytest.mark.asyncio
async def test_role_report_includes_correction_rate_per_role(auditor, db):
    """Correction rate dihitung per-role, konsisten dengan calibration_report
    tapi dipecah per agent, bukan per complexity label."""
    route = _fake_route()
    e1 = await auditor.log_decision("s_role3", "qa", "q1", route)
    await auditor.finalize(e1, _FakeTurn())
    e2 = await auditor.log_decision("s_role3", "qa", "q2", route)
    await auditor.finalize(e2, _FakeTurn())
    await auditor.check_correction("salah, coba lagi", "s_role3")

    report = await auditor.role_report()
    qa_row = [r for r in report if r["role"] == "qa"][0]
    assert qa_row["total"] == 2
    assert qa_row["corrections"] == 1
    assert qa_row["correction_rate"] == 50.0


@pytest.mark.asyncio
async def test_role_report_avg_human_feedback_null_when_none_given(auditor):
    """Role tanpa feedback sama sekali -> avg_human_feedback NULL, bukan 0
    (0 akan salah tafsir sebagai rating buruk, padahal 'tidak ada data')."""
    route = _fake_route()
    eid = await auditor.log_decision("s_role4", "pm", "q", route)
    await auditor.finalize(eid, _FakeTurn())

    report = await auditor.role_report()
    pm_row = [r for r in report if r["role"] == "pm"][0]
    assert pm_row["avg_human_feedback"] is None


@pytest.mark.asyncio
async def test_role_report_avg_human_feedback_computed_when_given(auditor):
    """Setelah set_human_feedback, avg_human_feedback terhitung — hanya dari
    event yang PUNYA feedback (bukan rata-rata semua turn termasuk NULL)."""
    route = _fake_route()
    e1 = await auditor.log_decision("s_role5", "pm", "q1", route)
    await auditor.finalize(e1, _FakeTurn())
    e2 = await auditor.log_decision("s_role5", "pm", "q2", route)
    await auditor.finalize(e2, _FakeTurn())

    await auditor.set_human_feedback(e1, 5)
    await auditor.set_human_feedback(e2, 3)

    report = await auditor.role_report()
    pm_row = [r for r in report if r["role"] == "pm"][0]
    assert pm_row["avg_human_feedback"] == 4.0


# ── set_human_feedback ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_human_feedback_stores_rating(auditor, db):
    route = _fake_route()
    eid = await auditor.log_decision("s_fb1", "pm", "q", route)
    await auditor.finalize(eid, _FakeTurn())

    ok = await auditor.set_human_feedback(eid, 4)

    assert ok is True
    row = await db.fetchone("SELECT human_feedback FROM routing_events WHERE id=?", (eid,))
    assert row["human_feedback"] == 4


@pytest.mark.asyncio
async def test_set_human_feedback_rejects_out_of_range(auditor):
    route = _fake_route()
    eid = await auditor.log_decision("s_fb2", "pm", "q", route)
    await auditor.finalize(eid, _FakeTurn())

    assert await auditor.set_human_feedback(eid, 0) is False
    assert await auditor.set_human_feedback(eid, 6) is False


@pytest.mark.asyncio
async def test_set_human_feedback_unknown_event_returns_false(auditor):
    assert await auditor.set_human_feedback(999999, 5) is False


@pytest.mark.asyncio
async def test_set_human_feedback_writes_to_audit_chain(auditor, db):
    """§ Prioritas 9.1 follow-up (a): rating eksplisit HARUS dirantai — bukti
    kualitas tindakan agent, bukan cuma sinyal kalibrasi."""
    route = _fake_route()
    eid = await auditor.log_decision("s_fb_chain", "pm", "q", route)
    await auditor.finalize(eid, _FakeTurn())

    await auditor.set_human_feedback(eid, 2)

    row = await db.fetchone(
        "SELECT ref_id, payload_json FROM audit_chain "
        "WHERE entry_type='routing.human_feedback' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["ref_id"] == eid
    assert '"rating":2' in row["payload_json"]


@pytest.mark.asyncio
async def test_set_human_feedback_out_of_range_does_not_write_to_chain(auditor, db):
    route = _fake_route()
    eid = await auditor.log_decision("s_fb_bad", "pm", "q", route)
    await auditor.finalize(eid, _FakeTurn())

    await auditor.set_human_feedback(eid, 99)

    row = await db.fetchone("SELECT id FROM audit_chain WHERE entry_type='routing.human_feedback'")
    assert row is None


@pytest.mark.asyncio
async def test_set_human_feedback_unknown_event_does_not_write_to_chain(auditor, db):
    await auditor.set_human_feedback(999999, 5)

    row = await db.fetchone("SELECT id FROM audit_chain WHERE entry_type='routing.human_feedback'")
    assert row is None


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


# ── cost_savings_report (§ Prioritas 9.4) ────────────────────────────────────


def _route_with_model(model: str, provider: str) -> RouteDecision:
    r = _fake_route()
    return RouteDecision(
        model=model,
        provider=provider,
        complexity=r.complexity,
        complexity_score=r.complexity_score,
        reason=r.reason,
        cost_per_1k=0.0,
        dimensions=r.dimensions,
        soul_upgrade_hit=r.soul_upgrade_hit,
    )


@pytest.mark.asyncio
async def test_cost_savings_report_empty(auditor):
    report = await auditor.cost_savings_report()
    assert report["turns_counted"] == 0
    assert report["turns_unpriced"] == 0
    assert report["actual_cost_usd"] == 0
    assert report["estimated_savings_usd"] == 0
    assert report["is_estimate"] is True


@pytest.mark.asyncio
async def test_cost_savings_report_does_not_use_stored_cost_usd_column(auditor):
    """cost_usd tersimpan SELALU 0.0 (cost_per_1k router selalu 0.0) — report
    HARUS menghitung ulang dari model_chosen+token, bukan membaca kolom itu.
    Test ini akan gagal kalau seseorang 'menyederhanakan' report jadi
    SUM(cost_usd) di masa depan."""
    route = _route_with_model("gemini-2.5-pro", "gemini")
    eid = await auditor.log_decision("s_cost1", "dev", "critical task", route)
    # cost_usd yang tersimpan = cost_per_1k(0.0) * tokens — selalu 0, apa pun tokennya.
    await auditor.finalize(eid, _FakeTurn(tokens_in=1_000_000, tokens_out=1_000_000, cost_usd=0.0))

    report = await auditor.cost_savings_report()
    # Kalau report salah membaca cost_usd, actual_cost_usd akan 0 di sini juga.
    assert report["actual_cost_usd"] == 11.25  # (1.25 + 10.00) per gemini-2.5-pro


@pytest.mark.asyncio
async def test_cost_savings_report_shows_savings_for_cheaper_tier(auditor):
    """Query yang dilayani model lokal gratis dibanding baseline CRITICAL
    (gemini-2.5-pro) harus menghasilkan penghematan > 0."""
    route = _route_with_model("gemma4:e4b", "ollama")
    eid = await auditor.log_decision("s_cost2", "pm", "hi", route)
    await auditor.finalize(eid, _FakeTurn(tokens_in=1_000_000, tokens_out=1_000_000))

    report = await auditor.cost_savings_report()
    assert report["turns_counted"] == 1
    assert report["actual_cost_usd"] == 0.0
    assert report["baseline_model"] == "gemini-2.5-pro"
    assert report["counterfactual_cost_usd"] == 11.25
    assert report["estimated_savings_usd"] == 11.25
    assert report["estimated_savings_pct"] == 100.0


@pytest.mark.asyncio
async def test_cost_savings_report_excludes_unpriced_models():
    """Model di luar tabel harga dikeluarkan dari agregat (turns_unpriced),
    BUKAN dihitung seolah gratis — konsisten prinsip 'jangan tebak'."""
    db = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await db.conn()
    await conn.executescript(sql)
    await conn.commit()
    auditor = RoutingAuditor(db=db)

    route = _route_with_model("some-brand-new-model-not-priced", "gemini")
    eid = await auditor.log_decision("s_cost3", "dev", "q", route)
    await auditor.finalize(eid, _FakeTurn(tokens_in=100, tokens_out=100))

    report = await auditor.cost_savings_report()
    assert report["turns_counted"] == 0
    assert report["turns_unpriced"] == 1
    assert report["actual_cost_usd"] == 0.0
    await db.close()


@pytest.mark.asyncio
async def test_cost_savings_report_ignores_unfinished_turns(auditor):
    """Turn yang belum di-finalize (tokens masih NULL) tak boleh ikut
    dihitung — bukan diperlakukan sebagai 0 token."""
    route = _fake_route()
    await auditor.log_decision("s_cost4", "pm", "q", route)  # tanpa finalize

    report = await auditor.cost_savings_report()
    assert report["turns_counted"] == 0
    assert report["turns_unpriced"] == 0


@pytest.mark.asyncio
async def test_cost_savings_report_aggregates_across_multiple_turns(auditor):
    route_free = _route_with_model("gemma4:e4b", "ollama")
    route_paid = _route_with_model("gemini-2.5-flash", "gemini")

    eid1 = await auditor.log_decision("s_cost5", "pm", "q1", route_free)
    await auditor.finalize(eid1, _FakeTurn(tokens_in=1_000_000, tokens_out=0))
    eid2 = await auditor.log_decision("s_cost6", "dev", "q2", route_paid)
    await auditor.finalize(eid2, _FakeTurn(tokens_in=1_000_000, tokens_out=0))

    report = await auditor.cost_savings_report()
    assert report["turns_counted"] == 2
    assert report["actual_cost_usd"] == 0.150  # hanya turn kedua yang berbayar
    # Baseline: kedua turn dihitung SEOLAH gemini-2.5-pro (1.25/1M input)
    assert report["counterfactual_cost_usd"] == 2.50


# ── agent_identity / identity_report (§ Prioritas 9.2) ───────────────────────


@pytest.mark.asyncio
async def test_log_decision_stores_agent_identity_when_given(auditor, db):
    route = _fake_route()
    eid = await auditor.log_decision("s_id1", "dev", "q", route, agent_identity="dev@abc123def456")

    row = await db.fetchone("SELECT agent_identity FROM routing_events WHERE id=?", (eid,))
    assert row["agent_identity"] == "dev@abc123def456"


@pytest.mark.asyncio
async def test_log_decision_agent_identity_defaults_to_none(auditor, db):
    """Caller lama yang belum menghitung identitas → NULL, bukan error
    (backward-compat)."""
    route = _fake_route()
    eid = await auditor.log_decision("s_id2", "dev", "q", route)

    row = await db.fetchone("SELECT agent_identity FROM routing_events WHERE id=?", (eid,))
    assert row["agent_identity"] is None


@pytest.mark.asyncio
async def test_log_decision_writes_agent_identity_to_audit_chain(auditor, db):
    """Melengkapi § Prioritas 9.1: identitas HARUS ikut ke payload rantai, bukan
    cuma kolom DB biasa — supaya bisa dijawab lintas hash chain juga."""
    route = _fake_route()
    await auditor.log_decision("s_id3", "dev", "q", route, agent_identity="dev@abc123def456")

    row = await db.fetchone(
        "SELECT payload_json FROM audit_chain WHERE entry_type='routing.decision' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert "dev@abc123def456" in row["payload_json"]


@pytest.mark.asyncio
async def test_identity_report_empty(auditor):
    assert await auditor.identity_report() == []


@pytest.mark.asyncio
async def test_identity_report_excludes_null_identity(auditor):
    """Baris tanpa agent_identity (caller lama) TIDAK boleh muncul sebagai satu
    grup 'None' — laporan ini soal identitas yang DIKETAHUI."""
    route = _fake_route()
    await auditor.log_decision("s_id4", "dev", "q", route)  # tanpa agent_identity

    assert await auditor.identity_report() == []


@pytest.mark.asyncio
async def test_identity_report_groups_by_role_and_identity(auditor):
    route = _fake_route()
    await auditor.log_decision("s_id5", "dev", "q1", route, agent_identity="dev@aaa111")
    await auditor.log_decision("s_id6", "dev", "q2", route, agent_identity="dev@aaa111")
    await auditor.log_decision("s_id7", "dev", "q3", route, agent_identity="dev@bbb222")

    report = await auditor.identity_report()
    by_identity = {r["agent_identity"]: r["total"] for r in report}
    assert by_identity == {"dev@aaa111": 2, "dev@bbb222": 1}


@pytest.mark.asyncio
async def test_identity_report_config_change_shows_as_two_identities():
    """Simulasi nyata: role sama, soul.toml berubah di tengah → dua identitas
    berbeda untuk role yang sama, persis pertanyaan yang dijawab fitur ini."""
    from core.agent_identity import agent_identity as make_identity

    db = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await db.conn()
    await conn.executescript(sql)
    await conn.commit()
    auditor = RoutingAuditor(db=db)

    soul_v1 = {"tools": {"allowed": ["file_read"]}}
    soul_v2 = {"tools": {"allowed": ["file_read", "shell_run"]}}  # permission ditambah
    identity_v1 = make_identity("dev", soul_v1)
    identity_v2 = make_identity("dev", soul_v2)

    route = _fake_route()
    await auditor.log_decision("s_id8", "dev", "before change", route, agent_identity=identity_v1)
    await auditor.log_decision("s_id9", "dev", "after change", route, agent_identity=identity_v2)

    report = await auditor.identity_report()
    identities = {r["agent_identity"] for r in report}
    assert identities == {identity_v1, identity_v2}
    await db.close()
