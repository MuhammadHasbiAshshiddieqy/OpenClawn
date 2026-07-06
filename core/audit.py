import json

from infra.database import DatabaseManager
from core.router import RouteDecision

CORRECTION_SIGNALS = [
    # Indonesia
    "salah",
    "bukan itu",
    "coba lagi",
    "maksudku",
    "kurang tepat",
    "tidak benar",
    "ulangi",
    "keliru",
    "bukan begitu",
    "harusnya",
    # English (core harus locale-neutral, §1.5)
    "that's wrong",
    "thats wrong",
    "not what i",
    "try again",
    "incorrect",
    "i meant",
    "no, ",
    "redo",
    "not right",
    "should be",
]


class RoutingAuditor:
    """
    Inovasi 1: catat setiap keputusan routing + apakah terbukti tepat.
    log_decision dipanggil SEBELUM LLM call, finalize SESUDAH.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def log_decision(
        self, session_id: str, role: str, query: str, route: RouteDecision
    ) -> int:
        d = route.dimensions
        cursor = await self.db.execute(
            """
            INSERT INTO routing_events (
                session_id, role, query_text,
                dim_query_tokens, dim_has_tech_kw, dim_needs_multistep,
                dim_history_len, dim_role, dim_has_urgency,
                dim_needs_stream, dim_is_continuation, dim_soul_upgrade_hit,
                complexity_score, complexity_label,
                model_chosen, provider, routing_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                role,
                query,
                d["query_tokens"],
                d["has_tech_kw"],
                d["needs_multistep"],
                d["history_len"],
                d["role"],
                d["has_urgency"],
                d["needs_stream"],
                d["is_continuation"],
                d["soul_upgrade_hit"],
                route.complexity_score,
                route.complexity.value,
                route.model,
                route.provider,
                route.reason,
            ),
        )
        return cursor.lastrowid

    async def finalize(self, event_id: int, turn, evidence: dict | None = None) -> None:
        """Update tokens, cost, latency, fallback_used, dan evidence setelah turn selesai.

        `evidence` (opsional, § Evidence-Based Response TODO.md Prioritas 2): snapshot
        policy/skill/guardrail yang berlaku saat turn ini — disimpan sebagai JSON agar
        query-able via GET /evidence/{event_id}, bukan cuma tersirat lintas kolom lain.
        """
        await self.db.execute(
            """
            UPDATE routing_events
            SET tokens_in=?, tokens_out=?, cost_usd=?, latency_ms=?, fallback_used=?,
                evidence_json=?
            WHERE id=?
            """,
            (
                turn.tokens_in,
                turn.tokens_out,
                turn.cost_usd,
                turn.latency_ms,
                int(getattr(turn, "fallback_used", False)),
                json.dumps(evidence) if evidence is not None else None,
                event_id,
            ),
        )

    async def check_correction(self, user_message: str, session_id: str) -> bool:
        """
        Dipanggil di AWAL turn berikutnya. Deteksi apakah turn sebelumnya dikoreksi user.

        Return True bila pesan ini mengoreksi turn sebelumnya (dipakai SkillFeedback
        untuk memutuskan outcome skill turn lalu: refine/reset vs revive/promote).
        """
        msg = user_message.lower()
        if not any(sig in msg for sig in CORRECTION_SIGNALS):
            return False
        await self.db.execute(
            """
            UPDATE routing_events SET had_correction=1, correction_detail=?
            WHERE id = (SELECT id FROM routing_events
                        WHERE session_id=? ORDER BY id DESC LIMIT 1)
            """,
            (user_message[:200], session_id),
        )
        return True

    async def calibration_report(self) -> list[dict]:
        """Complexity label mana yang sering memicu koreksi → router under-provisioned."""
        return await self.db.fetchall(
            """
            SELECT complexity_label,
                   COUNT(*) as total,
                   SUM(had_correction) as corrections,
                   ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate,
                   ROUND(AVG(cost_usd), 5) as avg_cost
            FROM routing_events
            GROUP BY complexity_label
            ORDER BY correction_rate DESC
            """
        )
