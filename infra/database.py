import aiosqlite
from infra.config import AppConfig


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
        """Jalankan migration SQL dari file."""
        with open(sql_path) as f:
            sql = f.read()
        db = await self.conn()
        await db.executescript(sql)
        await db.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
