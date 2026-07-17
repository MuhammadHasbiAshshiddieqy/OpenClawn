"""Prometheus text-exposition metrics (TODO.md § Prioritas 6).

Format teks manual (BUKAN library `prometheus_client`) — TODO.md eksplisit
"cukup untuk integrasi Grafana/Datadog tanpa SDK penuh dulu" (CLAUDE.md §8:
opsi ringan tanpa infra baru sebelum dependency berat). Semua metrik dibangun
dari data yang SUDAH ADA di DB (routing_events, tool_invocations, skills,
approval_log, users, autopilots) — tak ada tabel/kolom baru.

Spesifikasi format: https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from dataclasses import dataclass

from infra.database import DatabaseManager


@dataclass(frozen=True)
class _MetricFamily:
    name: str
    help_text: str
    metric_type: str  # "counter" | "gauge"


def _escape_label_value(value: str) -> str:
    """Escape backslash dan kutip ganda sesuai spesifikasi text-exposition —
    label value adalah string ter-quote, karakter mentah bisa memecah baris."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str]) -> str:
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _render_family(family: _MetricFamily, rows: list[tuple[dict[str, str], float]]) -> str:
    """Satu blok HELP+TYPE+baris nilai. Selalu render HELP/TYPE walau `rows`
    kosong (cardinality nol) — scraper/dashboard tetap tahu metrik ini ada."""
    lines = [
        f"# HELP {family.name} {family.help_text}",
        f"# TYPE {family.name} {family.metric_type}",
    ]
    for labels, value in rows:
        label_str = _format_labels(labels) if labels else ""
        lines.append(f"{family.name}{label_str} {value}")
    return "\n".join(lines) + "\n"


async def render_prometheus_metrics(db: DatabaseManager) -> str:
    """Render seluruh metric OpenCLAWN dalam format text-exposition Prometheus.

    Dipanggil oleh `GET /metrics/prometheus` (web/main.py). Query murni SELECT
    agregat — tak ada state, aman dipanggil berulang oleh scraper (default
    Prometheus: tiap 15-60 detik)."""
    blocks: list[str] = []

    routing_rows = await db.fetchall(
        """SELECT complexity_label, role,
                  COUNT(*) as total,
                  SUM(had_correction) as corrections,
                  SUM(cost_usd) as cost_sum
           FROM routing_events
           GROUP BY complexity_label, role"""
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_routing_events_total",
                "Total routing decisions made, by complexity tier and role.",
                "counter",
            ),
            [
                (
                    {"complexity_label": r["complexity_label"] or "", "role": r["role"] or ""},
                    r["total"],
                )
                for r in routing_rows
            ],
        )
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_routing_corrections_total",
                "Total routing decisions later corrected by the user, by complexity tier and role.",
                "counter",
            ),
            [
                (
                    {"complexity_label": r["complexity_label"] or "", "role": r["role"] or ""},
                    r["corrections"] or 0,
                )
                for r in routing_rows
            ],
        )
    )
    cost_by_role: dict[str, float] = {}
    for r in routing_rows:
        role = r["role"] or ""
        cost_by_role[role] = cost_by_role.get(role, 0.0) + (r["cost_sum"] or 0.0)
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_routing_cost_usd_total",
                "Total estimated LLM cost in USD, by role.",
                "counter",
            ),
            [({"role": role}, round(cost, 6)) for role, cost in cost_by_role.items()],
        )
    )

    tool_rows = await db.fetchall(
        """SELECT tool_name, outcome, COUNT(*) as total
           FROM tool_invocations
           GROUP BY tool_name, outcome"""
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_tool_invocations_total",
                "Total tool executions, by tool name and outcome.",
                "counter",
            ),
            [
                ({"tool_name": r["tool_name"] or "", "outcome": r["outcome"] or ""}, r["total"])
                for r in tool_rows
            ],
        )
    )

    skill_rows = await db.fetchall(
        """SELECT role, status, COUNT(*) as total
           FROM skills
           GROUP BY role, status"""
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_skills_total",
                "Current skill count, by role and lifecycle status.",
                "gauge",
            ),
            [
                ({"role": r["role"] or "", "status": r["status"] or ""}, r["total"])
                for r in skill_rows
            ],
        )
    )

    # `decision` bisa berisi baris historis lama pra-Human-Approval-Pipeline
    # (TODO.md § Prioritas 2) berformat "pending:{approval_id}" — approval_id
    # dulu di-encode ke kolom ini sebelum kolom `approval_log.approval_id`
    # terpisah ada. Baris seperti itu punya cardinality TAK TERBATAS (satu label
    # unik per approval_id) kalau tak dinormalisasi — kode SAAT INI selalu
    # menulis "pending" polos (lihat security/approval.py::request()), jadi ini
    # murni jaring pengaman untuk DB lama, bukan perilaku yang diharapkan terus muncul.
    approval_rows = await db.fetchall(
        "SELECT decision, COUNT(*) as total FROM approval_log GROUP BY decision"
    )
    approval_counts: dict[str, int] = {}
    for r in approval_rows:
        decision = r["decision"] or ""
        normalized = "pending" if decision.startswith("pending:") else decision
        approval_counts[normalized] = approval_counts.get(normalized, 0) + r["total"]
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_approval_log_total",
                "Total HITL approval requests recorded, by decision outcome.",
                "gauge",
            ),
            [({"decision": decision}, total) for decision, total in approval_counts.items()],
        )
    )

    user_rows = await db.fetchall(
        "SELECT access_role, COUNT(*) as total FROM users GROUP BY access_role"
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_users_total",
                "Current user count, by RBAC access role (TODO.md Prioritas 5).",
                "gauge",
            ),
            [({"access_role": r["access_role"] or ""}, r["total"]) for r in user_rows],
        )
    )

    autopilot_rows = await db.fetchall(
        "SELECT enabled, COUNT(*) as total FROM autopilots GROUP BY enabled"
    )
    blocks.append(
        _render_family(
            _MetricFamily(
                "openclawn_autopilots_total",
                "Current scheduled autopilot count, by enabled state.",
                "gauge",
            ),
            [
                ({"enabled": "true" if r["enabled"] else "false"}, r["total"])
                for r in autopilot_rows
            ],
        )
    )

    return "\n".join(blocks)
