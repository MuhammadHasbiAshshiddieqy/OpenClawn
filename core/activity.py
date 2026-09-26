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

# Audit 2026-09-26: sesi milik seorang user = sesi chat single-agent miliknya
# ATAU percakapan multi-agent miliknya. Dipakai memfilter blocker (yang hanya
# punya session_id) untuk non-admin.
OWNED_SESSIONS_SQL = (
    "session_id IN (SELECT session_id FROM chat_sessions WHERE owner_user_id = ?"
    " UNION SELECT session_id FROM conversations WHERE owner_user_id = ?)"
)


class ActivityTimeline:
    """Gabungkan peristiwa lintas tabel jadi satu linimasa terurut waktu (terbaru dulu).

    Tiap peristiwa diseragamkan ke bentuk: kind, role, title, detail, created_at.
    Filter `role` opsional → fokus pada satu peran (padanan "agent profile" Multica).
    Filter `owner_user_id` (audit 2026-09-26) → hanya peristiwa milik user itu,
    dipakai web untuk non-admin saat auth aktif. Sebelumnya /activity menampilkan
    prompt percakapan & detail blocker SEMUA user ke member mana pun.
    """

    # Peta jenis peristiwa → label tampil. Disimpan di sini agar UI tinggal pakai.
    # Framing "evidence" (§UI-IMPROVEMENT Cyber Detective): linimasa dibaca sebagai
    # berkas bukti audit, bukan cuma log — cocok dengan positioning audit-first.
    KINDS = {
        "route": "Routing evidence",
        "tool": "Tool evidence",
        "handoff": "Handoff evidence",
        "conversation": "Conversation evidence",
        "crystallize": "Crystallize evidence",
        "blocker": "Blocker",
    }

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def recent(
        self,
        role: str | None = None,
        limit: int = DEFAULT_LIMIT,
        owner_user_id: str | None = None,
    ) -> list[dict]:
        """Linimasa terbaru. `role=None` → semua peran. Fail-soft: tabel hilang → lewati sumber.

        Memakai UNION ALL antar-sumber lalu urut global agar paginasi konsisten.
        Tiap baris: {kind, role, title, detail, outcome, created_at}.
        `owner_user_id` None → tanpa filter kepemilikan (admin / auth nonaktif).
        """
        events: list[dict] = []
        owner = owner_user_id

        # 1. Routing: tiap keputusan model (dengan koreksi bila ada).
        events += await self._query(
            """SELECT 'route' AS kind, role,
                      complexity_label AS title,
                      model_chosen AS detail,
                      CASE had_correction WHEN 1 THEN 'corrected' ELSE 'ok' END AS outcome,
                      created_at
               FROM routing_events""",
            [("role = ?", role), ("user_id = ?", owner)],
            limit,
        )

        # 2. Tool: tiap eksekusi tool (outcome ok/error/timeout).
        events += await self._query(
            """SELECT 'tool' AS kind, role,
                      tool_name AS title,
                      '' AS detail,
                      outcome,
                      created_at
               FROM tool_invocations""",
            [("role = ?", role), ("user_id = ?", owner)],
            limit,
        )

        # 3. Handoff: kontrak antar-role (valid/degraded). Role = to_role (penerima).
        #    Hanya nama role/kontrak (tanpa isi) — tak difilter pemilik.
        events += await self._query(
            """SELECT 'handoff' AS kind, to_role AS role,
                      (from_role || ' → ' || to_role) AS title,
                      contract_name AS detail,
                      CASE validation_ok WHEN 1 THEN 'valid' ELSE 'degraded' END AS outcome,
                      created_at
               FROM role_handoffs""",
            [("to_role = ?", role)],
            limit,
        )

        # 4. Conversation: ringkasan run multi-agent. Tak punya kolom role tunggal →
        #    hanya muncul saat melihat SEMUA peran (role=None). `initial_message`
        #    adalah prompt user — difilter pemilik.
        if role is None:
            events += await self._query(
                """SELECT 'conversation' AS kind, pattern AS role,
                          (pattern || ' · ' || COALESCE(participants,'')) AS title,
                          COALESCE(initial_message,'') AS detail,
                          end_reason AS outcome,
                          created_at
                   FROM conversations""",
                [("owner_user_id = ?", owner)],
                limit,
            )

        # 5. Crystallize: percobaan menyimpan skill (active/draft/duplicate). Skill
        #    dibagi per role by design — tak difilter pemilik.
        events += await self._query(
            """SELECT 'crystallize' AS kind, role,
                      skill_name AS title,
                      reasoning AS detail,
                      status AS outcome,
                      created_at
               FROM crystallization_log""",
            [("role = ?", role)],
            limit,
        )

        # 6. Blocker: hambatan yang dilaporkan agent (open/resolved). Severity → outcome
        #    agar pewarnaan UI menonjolkan yang berat. Detail bisa memuat isi kerja user.
        events += await self._query(
            """SELECT 'blocker' AS kind, role,
                      summary AS title,
                      COALESCE(detail,'') AS detail,
                      (severity || '/' || status) AS outcome,
                      created_at
               FROM agent_blockers""",
            [("role = ?", role), (OWNED_SESSIONS_SQL, owner)],
            limit,
        )

        # Urut global terbaru-dulu; created_at TEXT ISO → urut leksikografis = kronologis.
        events.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        return events[:limit]

    async def _query(
        self, select_sql: str, filters: list[tuple[str, str | None]], limit: int
    ) -> list[dict]:
        """Tambahkan klausa WHERE untuk filter yang nilainya tidak None, lalu jalankan.

        Tiap `?` di klausa diisi nilai filter yang sama (klausa pemilik blocker
        memakai dua placeholder). Tabel/kolom hilang → [] (fail-soft, linimasa
        observability tak boleh menjatuhkan halaman).
        """
        clauses: list[str] = []
        params: list = []
        for clause, value in filters:
            if value is None:
                continue
            clauses.append(clause)
            params += [value] * clause.count("?")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"{select_sql}{where} ORDER BY id DESC LIMIT ?"
        try:
            return await self.db.fetchall(sql, (*params, limit))
        except Exception:  # noqa: BLE001 — linimasa observability, sumber rusak jangan menjatuhkan
            return []
