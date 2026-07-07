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
        # Audit log format actor_is_agent (TODO.md § Prioritas 2, pola GitHub
        # control plane): user_id query-able terpisah dari session_id +
        # actor_is_agent eksplisit — memudahkan integrasi SIEM eksternal.
        ("user_id", "TEXT DEFAULT 'default'"),
        ("actor_is_agent", "INTEGER DEFAULT 1"),
        # Multi-Tenant (TODO.md § Prioritas 5) — lihat komentar approval_log.
        ("tenant_id", "TEXT DEFAULT 'default'"),
    ],
    "approval_log": [
        # Human Approval Pipeline (TODO.md § Prioritas 2): approval_id SEBELUMNYA
        # hanya tersirat sebagai substring sementara "pending:{id}" di kolom
        # decision, hilang setelah resolve() menimpanya jadi "approved"/"rejected".
        # Kolom sendiri agar bisa di-query lintas status via GET /approval/{id}.
        ("approval_id", "TEXT"),
        # Multi-Tenant (TODO.md § Prioritas 5, migrations/002_multi_tenant.sql):
        # fondasi skema — default 'default' untuk kompatibilitas mundur.
        # Kode query BELUM di-filter per-tenant untuk tabel ini (lihat komentar
        # migrations/002_multi_tenant.sql untuk scope wiring saat ini).
        ("tenant_id", "TEXT DEFAULT 'default'"),
    ],
    "tool_invocations": [
        # Audit log format actor_is_agent (TODO.md § Prioritas 2) — sama seperti
        # routing_events, lihat komentar di atas.
        ("user_id", "TEXT DEFAULT 'default'"),
        ("actor_is_agent", "INTEGER DEFAULT 1"),
    ],
    "memory_l2": [
        # Multi-Tenant (TODO.md § Prioritas 5) — lihat komentar approval_log.
        ("tenant_id", "TEXT DEFAULT 'default'"),
    ],
    "chat_sessions": [
        # Multi-Tenant (TODO.md § Prioritas 5): WIRED PENUH — ChatSessionStore
        # benar-benar filter per-tenant (bukan cuma kolom pasif seperti tabel
        # multi-tenant lain di atas). Lihat migrations/002_multi_tenant.sql.
        ("tenant_id", "TEXT DEFAULT 'default'"),
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
        # Multi-Tenant (TODO.md § Prioritas 5): index composite dengan tenant_id
        # untuk chat_sessions — DB baru sudah dapat ini dari migrations/001_initial.sql
        # langsung, tapi IF NOT EXISTS aman dijalankan lagi (no-op untuk DB baru).
        # Untuk DB LAMA, index lama TANPA tenant_id (idx_chat_sessions_active dari
        # skema pra-multi-tenant) mungkin masih ada — dibiarkan (tak dihapus,
        # menghindari operasi destruktif yang tak perlu), index baru ini melengkapinya.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant_active "
            "ON chat_sessions(tenant_id, deleted_at, updated_at DESC)"
        )
        # memory_l2.tenant_id ditambal via _ADDED_COLUMNS di atas — index yang
        # mereferensikannya HARUS menunggu sampai baris ini (setelah ALTER TABLE
        # selesai), bukan statis di migrations/001_initial.sql (lihat komentar
        # migration file — pola sama idx_approval_id, gagal "no such column"
        # untuk DB lama bila dibuat lebih dulu).
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_l2_role ON memory_l2(tenant_id, role, importance DESC)"
        )
        await db.commit()
        # Rebuild tabel (skills, memory_l1) — DILAKUKAN SETELAH semua ALTER TABLE
        # & index di atas, karena rebuild adalah operasi paling berat (CREATE+COPY+
        # DROP+RENAME) dan independen dari kolom yang ditambal via ALTER biasa.
        await self._rebuild_tables_for_multi_tenant()
        await db.commit()

    async def _rebuild_tables_for_multi_tenant(self) -> None:
        """Multi-Tenant (TODO.md § Prioritas 5) — rebuild `memory_l1` dan `skills`
        agar constraint UNIQUE menyertakan `tenant_id` (`UNIQUE(role, key)` →
        `UNIQUE(tenant_id, role, key)`, dst). SQLite tidak bisa ALTER constraint
        tabel existing, jadi dilakukan CREATE TABLE baru → COPY data (tenant_id
        default 'default' untuk baris lama) → DROP tabel lama → RENAME.

        Idempoten: dicek via PRAGMA table_info — bila `tenant_id` SUDAH ada di
        skema tabel, rebuild di-skip (sudah pernah dijalankan, aman dipanggil
        berkali-kali tiap startup seperti `_ensure_columns` lainnya).
        """
        db = await self.conn()

        async with db.execute("PRAGMA table_info(memory_l1)") as cursor:
            existing = {row[1] async for row in cursor}
        if "tenant_id" not in existing:
            await db.executescript(
                """
                CREATE TABLE memory_l1_new (
                    id INTEGER PRIMARY KEY, tenant_id TEXT DEFAULT 'default', role TEXT NOT NULL,
                    key TEXT NOT NULL, value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, role, key)
                );
                INSERT INTO memory_l1_new (id, tenant_id, role, key, value, updated_at)
                    SELECT id, 'default', role, key, value, updated_at FROM memory_l1;
                DROP TABLE memory_l1;
                ALTER TABLE memory_l1_new RENAME TO memory_l1;
                """
            )

        async with db.execute("PRAGMA table_info(skills)") as cursor:
            existing = {row[1] async for row in cursor}
        if "tenant_id" not in existing:
            # DB super-lama (pra-I1) mungkin belum punya trigger_pattern/visibility
            # sama sekali (tak ada di _ADDED_COLUMNS, kolom itu ada sejak awal proyek
            # tapi fixture test membuktikan skema minimal tanpa itu juga mungkin ada
            # di lapangan) — SELECT hanya kolom yang benar-benar ada, biar tak "no
            # such column", nilai yang hilang jatuh ke DEFAULT tabel baru.
            select_cols = []
            insert_cols = []
            for col, default_expr in [
                ("id", "id"),
                ("role", "role"),
                ("skill_name", "skill_name"),
                ("trigger_pattern", "trigger_pattern" if "trigger_pattern" in existing else "NULL"),
                ("skill_content", "skill_content"),
                ("visibility", "visibility" if "visibility" in existing else "'private'"),
                ("status", "status" if "status" in existing else "'active'"),
                ("confidence", "confidence" if "confidence" in existing else "0.0"),
                ("generator_model", "generator_model" if "generator_model" in existing else "NULL"),
                ("use_count", "use_count" if "use_count" in existing else "0"),
                ("last_used_at", "last_used_at" if "last_used_at" in existing else "NULL"),
                ("decay_score", "decay_score" if "decay_score" in existing else "1.0"),
                (
                    "created_at",
                    "created_at" if "created_at" in existing else "CURRENT_TIMESTAMP",
                ),
                ("merged_into", "merged_into" if "merged_into" in existing else "NULL"),
                ("version", "version" if "version" in existing else "1"),
                (
                    "draft_success_count",
                    "draft_success_count" if "draft_success_count" in existing else "0",
                ),
            ]:
                insert_cols.append(col)
                select_cols.append(default_expr)
            insert_list = ", ".join(insert_cols)
            select_list = ", ".join(select_cols)
            await db.executescript(
                f"""
                CREATE TABLE skills_new (
                    id INTEGER PRIMARY KEY, tenant_id TEXT DEFAULT 'default', role TEXT NOT NULL,
                    skill_name TEXT NOT NULL, trigger_pattern TEXT, skill_content TEXT NOT NULL,
                    visibility TEXT DEFAULT 'private',
                    status TEXT DEFAULT 'active',
                    confidence REAL DEFAULT 0.0,
                    generator_model TEXT,
                    use_count INTEGER DEFAULT 0,
                    last_used_at TIMESTAMP,
                    decay_score REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    merged_into INTEGER REFERENCES skills(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    draft_success_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(tenant_id, role, skill_name)
                );
                INSERT INTO skills_new (tenant_id, {insert_list})
                    SELECT 'default', {select_list} FROM skills;
                DROP TABLE skills;
                ALTER TABLE skills_new RENAME TO skills;
                CREATE INDEX IF NOT EXISTS idx_skills_active
                    ON skills(tenant_id, role, status, decay_score DESC);
                """
            )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
