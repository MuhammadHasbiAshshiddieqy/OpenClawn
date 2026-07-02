"""Metadata sesi chat single-agent untuk sidebar riwayat (§ user report: "chat
selalu ke-reset", tak ada cara membuka chat baru/lanjutkan/hapus riwayat).

Akar masalah lama: `session_id` di-generate ulang (uuid acak) SETIAP kali
halaman `/` di-load — tak pernah disimpan di browser, jadi refresh selalu
terasa seperti chat baru walau `session_turns` sudah menyimpan transkrip.
Perbaikannya di sisi frontend (localStorage, lihat `chat.js`); modul ini
menyediakan metadata (judul, waktu, role) yang dibaca sidebar.

Judul di-generate LLM lokal (gemma4:e2b, sama tier `compaction_local_model`)
dari POTONGAN pesan pertama — bukan pesan penuh (§ user request: pesan awal
bisa panjang, jangan lempar semuanya cuma untuk judul). `title` NULL sampai
turn pertama selesai; sidebar menampilkan potongan mentah sebagai fallback
sementara generate berjalan di background.
"""

from infra.database import DatabaseManager

MAX_TITLE_CHARS = 60
# Ambil N kata pertama + M kata terakhir dari pesan panjang sebagai input
# generator judul (§ user request) — LLM kecil tetap dapat konteks awal DAN
# akhir (topik kadang baru jelas di akhir paragraf) tanpa membayar token untuk
# seluruh pesan. Pesan pendek (di bawah ambang ini) dikirim utuh, tak dipotong.
TITLE_INPUT_HEAD_WORDS = 20
TITLE_INPUT_TAIL_WORDS = 10


def truncate_for_title_prompt(message: str) -> str:
    """Potong pesan jadi `head...tail` bila melebihi head+tail kata; utuh bila tidak.

    Murni fungsi string — tanpa I/O — agar mudah diuji terpisah dari pemanggilan LLM.
    """
    words = message.split()
    limit = TITLE_INPUT_HEAD_WORDS + TITLE_INPUT_TAIL_WORDS
    if len(words) <= limit:
        return message
    head = " ".join(words[:TITLE_INPUT_HEAD_WORDS])
    tail = " ".join(words[-TITLE_INPUT_TAIL_WORDS:])
    return f"{head} ... {tail}"


class ChatSessionStore:
    """CRUD metadata sesi chat + daftar untuk sidebar (grouping waktu, per-role)."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def ensure_created(self, session_id: str, role: str) -> None:
        """Daftarkan sesi baru bila belum ada — idempoten (INSERT OR IGNORE).

        Dipanggil di awal `/chat/stream` SEBELUM turn jalan, agar sesi muncul di
        sidebar bahkan jika turn pertama gagal/timeout (user tetap lihat sesi
        "kosong" alih-alih hilang total).
        """
        await self.db.execute(
            "INSERT OR IGNORE INTO chat_sessions (session_id, role) VALUES (?, ?)",
            (session_id, role),
        )

    async def touch(self, session_id: str) -> None:
        """Perbarui `updated_at` — dipanggil tiap turn agar urutan sidebar (terbaru
        dulu) mencerminkan aktivitas terakhir, bukan cuma waktu dibuat."""
        await self.db.execute(
            "UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
            (session_id,),
        )

    async def set_title(self, session_id: str, title: str) -> None:
        title = title.strip().strip('"').strip("'")
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
        await self.db.execute(
            "UPDATE chat_sessions SET title=? WHERE session_id=?", (title, session_id)
        )

    async def has_title(self, session_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT title FROM chat_sessions WHERE session_id=?", (session_id,)
        )
        return bool(row and row["title"])

    async def list_active(self, limit: int = 200) -> list[dict]:
        """Sesi belum dihapus, terbaru dulu — mentah untuk sidebar mengelompokkan
        sendiri (per-waktu DAN per-role, § user request keduanya)."""
        rows = await self.db.fetchall(
            """SELECT session_id, role, title, created_at, updated_at
               FROM chat_sessions WHERE deleted_at IS NULL
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def soft_delete(self, session_id: str) -> None:
        """Hapus dari sidebar (soft — metadata tetap ada untuk audit trail lama),
        TAPI transkrip (`session_turns`) dan folder aktif (`session_workspace`)
        dihapus FISIK — user minta "hapus", isi percakapan harus benar hilang."""
        await self.db.execute(
            "UPDATE chat_sessions SET deleted_at=CURRENT_TIMESTAMP WHERE session_id=?",
            (session_id,),
        )
        await self.db.execute("DELETE FROM session_turns WHERE session_id=?", (session_id,))
        await self.db.execute("DELETE FROM session_workspace WHERE session_id=?", (session_id,))
