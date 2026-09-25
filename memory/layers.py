from infra.database import DatabaseManager
from infra.logging import log
from memory.search import fts5_query

SPECIFIC_TERMS = ["bug", "error", "oauth", "api", "deploy", "fix", "crash"]

# Audit 2026-09-25 (#2, kritis): checkpoint L1 SEBELUMNYA satu baris per ROLE
# (`key='last_summary'`) — 500 char jawaban terakhir user A disuntik ke prompt
# user B yang memakai role sama, tiap turn, tanpa serangan apa pun. Sekarang
# per SESI: key `last_summary:<session_id>`, dan baris `last_summary` global
# lama diabaikan saat membaca (tak dihapus — bukan data yang boleh dibuang diam-diam).
CHECKPOINT_KEY = "last_summary"
# Klausa SQL: baris L1 yang boleh dilihat sesi ini = key non-checkpoint (fakta
# role bersama) + checkpoint milik sesi ini sendiri. GLOB (bukan LIKE) karena
# '_' di "last_summary" adalah wildcard LIKE.
L1_VISIBLE_SQL = "(key = ? OR (key != 'last_summary' AND key NOT GLOB 'last_summary:*'))"


def checkpoint_key(session_id: str) -> str:
    return f"{CHECKPOINT_KEY}:{session_id}"


def l4_owner_filter(user_id: str) -> tuple[str, tuple]:
    """Klausa SQL (+ parameter) yang membatasi arsip L4 ke sesi milik `user_id`.

    Audit 2026-09-25 (#2): arsip L4 SEBELUMNYA dicari lintas SEMUA sesi satu
    role — ringkasan percakapan user lain bisa muncul di prompt. `"default"` =
    auth nonaktif (single-user): semua sesi KECUALI yang tercatat milik user
    login tertentu. User login: hanya sesi yang `owner_user_id`-nya dia."""
    if not user_id or user_id == "default":
        return (
            "session_id NOT IN (SELECT session_id FROM chat_sessions "
            "WHERE owner_user_id IS NOT NULL)",
            (),
        )
    return (
        "session_id IN (SELECT session_id FROM chat_sessions WHERE owner_user_id = ?)",
        (user_id,),
    )


class MemoryManager:
    """L0-L4 memory management. L4 pakai FTS5 untuk cross-session search."""

    def __init__(self, role: str, session_id: str, db: DatabaseManager, user_id: str = "default"):
        self.role = role
        self.session_id = session_id
        self.db = db
        self.user_id = user_id

    async def load_context(self, query: str, skills: list) -> dict:
        own_key = checkpoint_key(self.session_id)
        l1_rows = await self.db.fetchall(
            f"SELECT key, value FROM memory_l1 WHERE role=? AND {L1_VISIBLE_SQL} LIMIT 20",
            (self.role, own_key),
        )
        # Checkpoint sesi ini ditampilkan dengan nama lama "last_summary" — konsumen
        # (compactor/prompt) tak perlu tahu skema key per-sesi.
        l1 = {(CHECKPOINT_KEY if r["key"] == own_key else r["key"]): r["value"] for r in l1_rows}

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
            owner_sql, owner_params = l4_owner_filter(self.user_id)
            try:
                l4_rows = await self.db.fetchall(
                    f"""SELECT summary FROM memory_l4
                       WHERE role=? AND memory_l4 MATCH ? AND {owner_sql}
                       ORDER BY rank LIMIT 3""",
                    (self.role, match, *owner_params),
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
            """INSERT INTO memory_l1 (role, key, value) VALUES (?, ?, ?)
               ON CONFLICT(tenant_id, role, key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (self.role, checkpoint_key(self.session_id), summary[:500]),
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
