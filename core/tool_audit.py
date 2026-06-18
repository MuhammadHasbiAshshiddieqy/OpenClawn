"""Telemetri penggunaan tool — audit untuk tooling (setara Inovasi 1 untuk routing).

Mencatat setiap eksekusi tool di titik terpusat (AgentLoop._execute_tool): tool apa,
role mana, hasil (ok/error/timeout), latency. Menjawab "tool mana yang berguna, mana
yang sering gagal". Murni DB-bound (hanya DatabaseManager, §1.6) — bisa diekstrak.

record() fail-soft: kegagalan menulis telemetri TIDAK boleh menjatuhkan turn agent.
Telemetri adalah pengamatan, bukan jalur kritis.
"""

from infra.database import DatabaseManager
from infra.logging import log


class ToolAudit:
    """Tulis & agregasi catatan penggunaan tool."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def record(
        self, session_id: str, role: str, tool_name: str, outcome: str, latency_ms: int
    ) -> None:
        """Catat satu eksekusi tool. Fail-soft: error tulis hanya di-log, tidak diteruskan."""
        try:
            await self.db.execute(
                """INSERT INTO tool_invocations (session_id, role, tool_name, outcome, latency_ms)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, role, tool_name, outcome, latency_ms),
            )
        except Exception as exc:  # noqa: BLE001 — telemetri tak boleh ganggu turn
            log.warning("tool_audit_write_failed", tool=tool_name, error=str(exc))

    async def summary(self) -> list[dict]:
        """Agregasi per tool untuk /metrics: total, gagal, timeout, rate, avg latency.

        Diurut paling sering dipakai dulu — itu yang paling relevan ditinjau.
        """
        return await self.db.fetchall(
            """SELECT
                 tool_name,
                 COUNT(*) AS total,
                 SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors,
                 SUM(CASE WHEN outcome='timeout' THEN 1 ELSE 0 END) AS timeouts,
                 ROUND(100.0 * SUM(CASE WHEN outcome!='ok' THEN 1 ELSE 0 END) / COUNT(*), 1)
                   AS fail_rate,
                 ROUND(AVG(latency_ms)) AS avg_latency_ms
               FROM tool_invocations
               GROUP BY tool_name
               ORDER BY total DESC"""
        )
