"""Test untuk core/prometheus_metrics.py (TODO.md § Prioritas 6).

Format text-exposition Prometheus murni (tanpa dependency `prometheus_client` —
TODO.md eksplisit "cukup untuk integrasi Grafana/Datadog tanpa SDK penuh dulu").
Test memverifikasi bentuk output (HELP/TYPE/label/nilai) sesuai spesifikasi
text-exposition format, bukan mem-parse balik dengan library eksternal.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from core.prometheus_metrics import render_prometheus_metrics


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


def _lines_without_comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


@pytest.mark.asyncio
async def test_empty_db_produces_valid_output_no_crash(db):
    """DB kosong (belum ada turn/tool/skill sama sekali) tak boleh crash —
    metric dengan cardinality nol cukup HELP+TYPE tanpa baris nilai."""
    text = await render_prometheus_metrics(db)
    assert isinstance(text, str)
    assert "# HELP openclawn_routing_events_total" in text
    assert "# TYPE openclawn_routing_events_total counter" in text


@pytest.mark.asyncio
async def test_routing_events_counted_by_label_and_role(db):
    await db.execute(
        """INSERT INTO routing_events (session_id, role, complexity_label, had_correction, cost_usd, query_text)
           VALUES ('s1','pm','simple',0,0.001,'q')"""
    )
    await db.execute(
        """INSERT INTO routing_events (session_id, role, complexity_label, had_correction, cost_usd, query_text)
           VALUES ('s2','pm','simple',1,0.002,'q')"""
    )
    await db.execute(
        """INSERT INTO routing_events (session_id, role, complexity_label, had_correction, cost_usd, query_text)
           VALUES ('s3','qa','complex',0,0.05,'q')"""
    )
    text = await render_prometheus_metrics(db)

    assert 'openclawn_routing_events_total{complexity_label="simple",role="pm"} 2' in text
    assert 'openclawn_routing_events_total{complexity_label="complex",role="qa"} 1' in text
    assert 'openclawn_routing_corrections_total{complexity_label="simple",role="pm"} 1' in text


@pytest.mark.asyncio
async def test_routing_cost_summed_per_role(db):
    await db.execute(
        """INSERT INTO routing_events (session_id, role, complexity_label, cost_usd, query_text)
           VALUES ('s1','pm','simple',0.001,'q')"""
    )
    await db.execute(
        """INSERT INTO routing_events (session_id, role, complexity_label, cost_usd, query_text)
           VALUES ('s2','pm','simple',0.002,'q')"""
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_routing_cost_usd_total{role="pm"} 0.003' in text


@pytest.mark.asyncio
async def test_tool_invocations_counted_by_name_and_outcome(db):
    await db.execute(
        """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms)
           VALUES ('s1','pm','file_read','success',12)"""
    )
    await db.execute(
        """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms)
           VALUES ('s2','pm','file_read','error',30)"""
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_tool_invocations_total{outcome="success",tool_name="file_read"} 1' in text
    assert 'openclawn_tool_invocations_total{outcome="error",tool_name="file_read"} 1' in text


@pytest.mark.asyncio
async def test_skills_counted_by_role_and_status(db):
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('pm','a','x','active')"
    )
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('pm','b','x','draft')"
    )
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('qa','c','x','active')"
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_skills_total{role="pm",status="active"} 1' in text
    assert 'openclawn_skills_total{role="pm",status="draft"} 1' in text
    assert 'openclawn_skills_total{role="qa",status="active"} 1' in text


@pytest.mark.asyncio
async def test_approval_log_counted_by_decision(db):
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s1','code_run','{}','approved')"
    )
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s2','code_run','{}','rejected')"
    )
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s3','code_run','{}','approved')"
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_approval_log_total{decision="approved"} 2' in text
    assert 'openclawn_approval_log_total{decision="rejected"} 1' in text


@pytest.mark.asyncio
async def test_approval_log_normalizes_legacy_pending_id_suffix(db):
    """Baris historis pra-Human-Approval-Pipeline (TODO.md § Prioritas 2) berformat
    'pending:{approval_id}' harus dinormalisasi ke 'pending' polos — tanpa ini,
    tiap approval_id unik jadi label cardinality baru (tak terbatas)."""
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s1','code_run','{}','pending:abc123')"
    )
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s2','code_run','{}','pending:def456')"
    )
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, tool_input, decision) VALUES ('s3','code_run','{}','pending')"
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_approval_log_total{decision="pending"} 3' in text
    assert "pending:abc123" not in text
    assert "pending:def456" not in text


@pytest.mark.asyncio
async def test_users_counted_by_access_role(db):
    await db.execute(
        "INSERT INTO users (tenant_id, subject, access_role) VALUES ('default','shared-secret','admin')"
    )
    await db.execute(
        "INSERT INTO users (tenant_id, subject, access_role) VALUES ('default','oidc-1','member')"
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_users_total{access_role="admin"} 1' in text
    assert 'openclawn_users_total{access_role="member"} 1' in text


@pytest.mark.asyncio
async def test_autopilots_counted_by_enabled_state(db):
    await db.execute(
        """INSERT INTO autopilots (name, role, prompt, interval_sec, enabled, next_run_at)
           VALUES ('a1','pm','do x',3600,1,datetime('now'))"""
    )
    await db.execute(
        """INSERT INTO autopilots (name, role, prompt, interval_sec, enabled, next_run_at)
           VALUES ('a2','pm','do y',3600,0,datetime('now'))"""
    )
    text = await render_prometheus_metrics(db)
    assert 'openclawn_autopilots_total{enabled="true"} 1' in text
    assert 'openclawn_autopilots_total{enabled="false"} 1' in text


@pytest.mark.asyncio
async def test_output_ends_with_newline_per_exposition_format(db):
    """Text-exposition format Prometheus mensyaratkan setiap baris (termasuk
    baris terakhir) diakhiri newline."""
    text = await render_prometheus_metrics(db)
    assert text.endswith("\n")


@pytest.mark.asyncio
async def test_label_values_escaped_against_injection(db):
    """Nama tool/role dengan karakter kutip/backslash tak boleh merusak format
    label Prometheus (mis. `"` di tool_name tak sengaja menutup label lebih awal)."""
    await db.execute(
        """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms)
           VALUES ('s1','pm','weird"tool\\name','success',5)"""
    )
    text = await render_prometheus_metrics(db)
    # Backslash dan kutip HARUS di-escape (\\ dan \"), baris tak boleh pecah.
    assert 'tool_name="weird\\"tool\\\\name"' in text


@pytest.mark.asyncio
async def test_help_and_type_present_for_every_metric_family(db):
    """Setiap metric family (nama unik) harus punya tepat satu blok HELP+TYPE,
    walau tak ada data (cardinality nol) — dashboard/scraper mengandalkan ini
    untuk menampilkan metrik yang tersedia."""
    text = await render_prometheus_metrics(db)
    for metric in (
        "openclawn_routing_events_total",
        "openclawn_routing_corrections_total",
        "openclawn_routing_cost_usd_total",
        "openclawn_tool_invocations_total",
        "openclawn_skills_total",
        "openclawn_approval_log_total",
        "openclawn_users_total",
        "openclawn_autopilots_total",
    ):
        assert f"# HELP {metric} " in text, f"missing HELP for {metric}"
        assert f"# TYPE {metric} " in text, f"missing TYPE for {metric}"
