import aiosqlite
from infra.config import AppConfig

# Kolom yang ditambahkan ke tabel yang SUDAH ADA setelah rilis awal (mis. Skill
# Curator/Compounding I1-I3). `CREATE TABLE IF NOT EXISTS` di migrations/001_initial.sql
# adalah no-op pada tabel existing, jadi kolom baru tak pernah muncul di DB lama →
# "no such column" saat runtime. Daftar ini di-cek tiap startup (idempoten, aman
# dijalankan berkali-kali) agar instalasi lama otomatis dapat kolom baru tanpa
# migrasi manual atau menghapus data.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "skills": [
        ("merged_into", "INTEGER REFERENCES skills(id)"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("draft_success_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "curation_log": [
        ("status", "TEXT NOT NULL DEFAULT 'applied'"),
        ("merged_content", "TEXT"),
    ],
    "routing_events": [
        # Evidence-Based Response (TODO.md § Prioritas 2): snapshot JSON dari
        # policy/skill/guardrail yang berlaku SAAT turn ini berjalan — data yang
        # sebelumnya cuma tersirat lintas kolom, sekarang satu payload query-able
        # via GET /evidence/{id}. Kompetitor besar (LangChain/CrewAI/AutoGen)
        # eksplisit belum ship ini (§ KESIMPULAN.md §2.2).
        ("evidence_json", "TEXT"),
        # Runtime Evaluation Engine (TODO.md § Prioritas 2): rating eksplisit
        # user (1-5) via POST /feedback/{event_id}. NULL = belum diberi feedback.
        ("human_feedback", "INTEGER"),
    ],
    "approval_log": [
        # Human Approval Pipeline (TODO.md § Prioritas 2): approval_id SEBELUMNYA
        # hanya tersirat sebagai substring sementara "pending:{id}" di kolom
        # decision, hilang setelah resolve() menimpanya jadi "approved"/"rejected".
        # Kolom sendiri agar bisa di-query lintas status via GET /approval/{id}.
        ("approval_id", "TEXT"),
    ],
}


class DatabaseManager:
    """
    Satu koneksi shared per proses. Di-pass ke semua modul via dependency injection.
    Daftarkan POWER(base, exp) sebagai custom function karena SQLite tidak punya bawaan —
    dibutuhkan untuk exponential decay di skill_decay.py.
    """

    def __init__(self, config: AppConfig):
        self._path = config.db_path
        self._conn: aiosqlite.Connection | None = None

    async def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            # POWER() dibutuhkan untuk exponential decay (§12)
            await self._conn.create_function("POWER", 2, lambda b, e: b**e)
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        db = await self.conn()
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        db = await self.conn()
        async with db.execute(sql, params) as cursor:
            return [dict(row) async for row in cursor]

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        db = await self.conn()
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def run_migration(self, sql_path: str) -> None:
        """Jalankan migration SQL dari file, lalu tambal kolom baru di tabel lama."""
        with open(sql_path) as f:
            sql = f.read()
        db = await self.conn()
        await db.executescript(sql)
        await db.commit()
        await self._ensure_columns()

    async def _ensure_columns(self) -> None:
        """Tambal kolom yang hilang di tabel EXISTING (lihat `_ADDED_COLUMNS`).

        `CREATE TABLE IF NOT EXISTS` tak menyentuh tabel yang sudah ada, jadi kolom
        yang ditambahkan setelah rilis awal tak pernah muncul di DB lama tanpa ini.
        Idempoten: hanya ALTER kolom yang benar-benar belum ada (PRAGMA table_info).
        """
        db = await self.conn()
        for table, columns in _ADDED_COLUMNS.items():
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row[1] async for row in cursor}
            for name, decl in columns:
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        await db.commit()

        # Index untuk kolom yang ditambal di atas — DIBUAT SETELAH ALTER TABLE
        # selesai (bukan statis di migrations/001_initial.sql) karena DB LAMA
        # baru mendapat kolomnya di baris atas; CREATE INDEX yang jalan lebih
        # dulu (di executescript sebelum _ensure_columns dipanggil) akan gagal
        # "no such column" untuk DB lama tanpa kolom ini sejak awal.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_approval_id ON approval_log(approval_id)")
        await db.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
