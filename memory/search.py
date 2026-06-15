"""FTS5 cross-session search. Fungsionalitas dasar ada di layers.py;
modul ini mengekspos interface standalone agar bisa diekstrak sebagai paket terpisah."""

from infra.database import DatabaseManager
from infra.logging import log

SPECIFIC_TERMS = ["bug", "error", "oauth", "api", "deploy", "fix", "crash"]


class SessionSearch:
    """Cross-session FTS5 search untuk memory L4."""

    def __init__(self, role: str, db: DatabaseManager):
        self.role = role
        self.db = db

    def should_search(self, query: str) -> bool:
        """Threshold adaptif: > 3 kata ATAU mengandung term teknis spesifik."""
        if len(query.split()) > 3:
            return True
        q = query.lower()
        return any(t in q for t in SPECIFIC_TERMS)

    async def search(self, query: str, limit: int = 3) -> list[str]:
        """Return daftar summary dari sesi lama yang relevan."""
        if not self.should_search(query):
            return []
        try:
            rows = await self.db.fetchall(
                """SELECT summary FROM memory_l4
                   WHERE role=? AND memory_l4 MATCH ? ORDER BY rank LIMIT ?""",
                (self.role, query, limit),
            )
            return [r["summary"] for r in rows]
        except Exception as e:
            # FTS5 syntax error atau table belum ada → skip gracefully, tapi tetap log.
            log.debug("fts5_search_skipped", role=self.role, error=str(e))
            return []

    async def archive(self, session_id: str, summary: str, full_content: str) -> None:
        """Simpan sesi selesai ke L4 untuk future search."""
        await self.db.execute(
            """INSERT INTO memory_l4 (role, session_id, summary, full_content, created_at)
               VALUES (?,?,?,?, datetime('now'))""",
            (self.role, session_id, summary, full_content),
        )
