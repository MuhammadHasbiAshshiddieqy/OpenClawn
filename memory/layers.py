from infra.database import DatabaseManager
from infra.logging import log
from memory.search import fts5_query

SPECIFIC_TERMS = ["bug", "error", "oauth", "api", "deploy", "fix", "crash"]


class MemoryManager:
    """L0-L4 memory management. L4 pakai FTS5 untuk cross-session search."""

    def __init__(self, role: str, session_id: str, db: DatabaseManager):
        self.role = role
        self.session_id = session_id
        self.db = db

    async def load_context(self, query: str, skills: list) -> dict:
        l1_rows = await self.db.fetchall(
            "SELECT key, value FROM memory_l1 WHERE role=? LIMIT 20", (self.role,)
        )
        l1 = {r["key"]: r["value"] for r in l1_rows}

        l2_rows = await self.db.fetchall(
            "SELECT fact FROM memory_l2 WHERE role=? ORDER BY importance DESC LIMIT 30",
            (self.role,),
        )
        l2 = [r["fact"] for r in l2_rows]

        # FTS5: trigger jika query > 3 kata ATAU mengandung kata teknis spesifik.
        # Threshold 5 kata terlalu kaku untuk query seperti "bug login OAuth" (audit)
        l4: list[str] = []
        match = fts5_query(query)
        if match and (len(query.split()) > 3 or self._has_specific_term(query)):
            try:
                l4_rows = await self.db.fetchall(
                    """SELECT summary FROM memory_l4
                       WHERE role=? AND memory_l4 MATCH ? ORDER BY rank LIMIT 3""",
                    (self.role, match),
                )
                l4 = [r["summary"] for r in l4_rows]
            except Exception as e:
                # Safety-net: harusnya tak terjadi lagi setelah fts5_query, tapi tetap
                # tangani agar query aneh tak meng-crash turn (CLAUDE.md §6).
                log.debug("fts5_load_skipped", role=self.role, error=str(e))

        return {"l1": l1, "l2": l2, "l3": skills, "l4": l4}

    def _has_specific_term(self, query: str) -> bool:
        q = query.lower()
        return any(t in q for t in SPECIFIC_TERMS)

    async def load_turns(self, limit: int = 20) -> list[dict]:
        """Muat transkrip giliran (user/assistant) sesi INI, urut lama→baru.

        Memperbaiki hilangnya konteks percakapan (§ user report: agent seolah tak
        pernah baca chat sebelumnya, bahkan di sesi yang sama). AgentLoop dibuat baru
        tiap request → self.history kosong; ini yang mengembalikan riwayat sesi dari
        DB agar build() menyertakannya ke messages. Di-cap `limit` giliran TERBARU
        (token-first §1.4) — compaction/truncation di build() menangani sisanya.
        """
        rows = await self.db.fetchall(
            """SELECT role, content FROM session_turns WHERE session_id=?
               ORDER BY id DESC LIMIT ?""",
            (self.session_id, limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def append_turn(self, role: str, content: str) -> None:
        """Simpan satu giliran (user/assistant) ke transkrip sesi (persist multi-turn)."""
        if not content:
            return
        await self.db.execute(
            "INSERT INTO session_turns (session_id, role, content) VALUES (?,?,?)",
            (self.session_id, role, content),
        )

    async def update_checkpoint(self, summary: str) -> None:
        await self.db.execute(
            """INSERT INTO memory_l1 (role, key, value) VALUES (?, 'last_summary', ?)
               ON CONFLICT(tenant_id, role, key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (self.role, summary[:500]),
        )

    async def add_fact(self, fact: str, importance: int = 1, locale: str = "neutral") -> None:
        await self.db.execute(
            "INSERT INTO memory_l2 (role, fact, importance, locale) VALUES (?,?,?,?)",
            (self.role, fact, importance, locale),
        )

    async def archive_session(self, summary: str, full_content: str) -> None:
        """Arsipkan sesi ke L4. Idempoten per sesi: ganti arsip lama session ini
        agar tidak menumpuk duplikat saat dipanggil berulang (FTS5 tak punya UNIQUE)."""
        await self.db.execute(
            "DELETE FROM memory_l4 WHERE role=? AND session_id=?",
            (self.role, self.session_id),
        )
        await self.db.execute(
            """INSERT INTO memory_l4 (role, session_id, summary, full_content, created_at)
               VALUES (?,?,?,?, datetime('now'))""",
            (self.role, self.session_id, summary, full_content),
        )
