"""Activity timeline — linimasa kronologis aksi agent lintas tabel.

Terinspirasi "Activity Timeline" Multica: melihat APA yang dilakukan agent dari
waktu ke waktu sebagai satu aliran, bukan terpencar di banyak halaman. Tidak ada
tabel baru — modul ini hanya MENGAGREGASI peristiwa yang sudah dicatat:
routing_events, tool_invocations, role_handoffs, conversations, crystallization_log.

Read-only & extractable (CLAUDE.md §1.6): hanya bergantung `DatabaseManager`.
"""

from infra.database import DatabaseManager

# Batas default item linimasa agar query & render tetap ringan (token/UI-first).
DEFAULT_LIMIT = 60


class ActivityTimeline:
    """Gabungkan peristiwa lintas tabel jadi satu linimasa terurut waktu (terbaru dulu).

    Tiap peristiwa diseragamkan ke bentuk: kind, role, title, detail, created_at.
    Filter `role` opsional → fokus pada satu peran (padanan "agent profile" Multica).
    """

    # Peta jenis peristiwa → label tampil. Disimpan di sini agar UI tinggal pakai.
    KINDS = {
        "route": "Routing",
        "tool": "Tool",
        "handoff": "Handoff",
        "conversation": "Conversation",
        "crystallize": "Crystallize",
        "blocker": "Blocker",
    }

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def recent(self, role: str | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Linimasa terbaru. `role=None` → semua peran. Fail-soft: tabel hilang → lewati sumber.

        Memakai UNION ALL antar-sumber lalu urut global agar paginasi konsisten.
        Tiap baris: {kind, role, title, detail, outcome, created_at}.
        """
        # Klausa filter role dibuat sekali; tiap sub-query memakai kolom role-nya sendiri.
        events: list[dict] = []

        # 1. Routing: tiap keputusan model (dengan koreksi bila ada).
        events += await self._safe(
            f"""SELECT 'route' AS kind, role,
                       complexity_label AS title,
                       model_chosen AS detail,
                       CASE had_correction WHEN 1 THEN 'corrected' ELSE 'ok' END AS outcome,
                       created_at
                FROM routing_events
                {self._where(role)}
                ORDER BY id DESC LIMIT ?""",
            role,
            limit,
        )

        # 2. Tool: tiap eksekusi tool (outcome ok/error/timeout).
        events += await self._safe(
            f"""SELECT 'tool' AS kind, role,
                       tool_name AS title,
                       '' AS detail,
                       outcome,
                       created_at
                FROM tool_invocations
                {self._where(role)}
                ORDER BY id DESC LIMIT ?""",
            role,
            limit,
        )

        # 3. Handoff: kontrak antar-role (valid/degraded). Role = to_role (penerima).
        role_clause = "WHERE to_role = ?" if role else ""
        events += await self._safe(
            f"""SELECT 'handoff' AS kind, to_role AS role,
                       (from_role || ' → ' || to_role) AS title,
                       contract_name AS detail,
                       CASE validation_ok WHEN 1 THEN 'valid' ELSE 'degraded' END AS outcome,
                       created_at
                FROM role_handoffs
                {role_clause}
                ORDER BY id DESC LIMIT ?""",
            role,
            limit,
        )

        # 4. Conversation: ringkasan run multi-agent. Tak punya kolom role tunggal →
        #    hanya muncul saat melihat SEMUA peran (role=None).
        if role is None:
            events += await self._safe(
                """SELECT 'conversation' AS kind, pattern AS role,
                          (pattern || ' · ' || COALESCE(participants,'')) AS title,
                          COALESCE(initial_message,'') AS detail,
                          end_reason AS outcome,
                          created_at
                   FROM conversations
                   ORDER BY id DESC LIMIT ?""",
                None,
                limit,
            )

        # 5. Crystallize: percobaan menyimpan skill (active/draft/duplicate).
        events += await self._safe(
            f"""SELECT 'crystallize' AS kind, role,
                       skill_name AS title,
                       reasoning AS detail,
                       status AS outcome,
                       created_at
                FROM crystallization_log
                {self._where(role)}
                ORDER BY id DESC LIMIT ?""",
            role,
            limit,
        )

        # 6. Blocker: hambatan yang dilaporkan agent (open/resolved). Severity → outcome
        #    agar pewarnaan UI menonjolkan yang berat.
        events += await self._safe(
            f"""SELECT 'blocker' AS kind, role,
                       summary AS title,
                       COALESCE(detail,'') AS detail,
                       (severity || '/' || status) AS outcome,
                       created_at
                FROM agent_blockers
                {self._where(role)}
                ORDER BY id DESC LIMIT ?""",
            role,
            limit,
        )

        # Urut global terbaru-dulu; created_at TEXT ISO → urut leksikografis = kronologis.
        events.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        return events[:limit]

    @staticmethod
    def _where(role: str | None) -> str:
        """Klausa WHERE untuk sumber yang punya kolom `role`."""
        return "WHERE role = ?" if role else ""

    async def _safe(self, sql: str, role: str | None, limit: int) -> list[dict]:
        """Jalankan sub-query; tabel/kolom hilang → kembalikan [] (fail-soft, tak menjatuhkan halaman)."""
        params: tuple = (role, limit) if role else (limit,)
        try:
            return await self.db.fetchall(sql, params)
        except Exception:  # noqa: BLE001 — linimasa observability, sumber rusak jangan menjatuhkan
            return []
