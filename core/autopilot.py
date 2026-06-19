"""Autopilots — tugas agent terjadwal yang berjalan otomatis.

Terinspirasi "Autopilots" Multica (audit harian, ringkasan berkala). DNA OpenCLAWN:
minimal & aman. Maka di sini:

- **Scheduler = loop asyncio in-process** (tanpa dependency baru, CLAUDE.md §7).
  Interval sederhana berbasis detik dalam UTC — TANPA cron/timezone/DST. Menyelesaikan
  90% kebutuhan (tiap N jam/hari) tanpa kompleksitas APScheduler.
- **Keamanan (§1, §17):** autopilot berjalan tanpa manusia di depan. AgentLoop
  dijalankan dengan `autopilot=True` → tool yang butuh approval TIDAK dieksekusi,
  melainkan diantri sebagai proposal (lihat AgentLoop._execute_tool). Tidak ada
  eksekusi destruktif diam-diam.
- **Misfire:** kebijakan sederhana — jalankan paling banyak SEKALI saat due, lalu
  jadwalkan ulang dari "sekarang". Jadwal terlewat (server mati) tidak menumpuk.

Extractable: `AutopilotStore` hanya bergantung `DatabaseManager`. `AutopilotScheduler`
menerima `runner` callable agar logika eksekusi (AgentLoop) disuntik dari web layer —
modul ini tak mengimpor web maupun memaksa AgentLoop.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from infra.config import CONFIG, AppConfig
from infra.database import DatabaseManager
from infra.logging import log

# Batas agar autopilot tak dibuat dengan interval ekstrem (token/biaya & spam).
MIN_INTERVAL_SEC = 60
# Runner: (autopilot_row) -> jumlah proposal yang diantri pada run itu.
AutopilotRunner = Callable[[dict], Awaitable[int]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Serialisasi ke 'YYYY-MM-DD HH:MM:SS' UTC (selaras CURRENT_TIMESTAMP SQLite)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class AutopilotStore:
    """CRUD jadwal autopilot + riwayat run. Hanya bergantung DatabaseManager."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(self, name: str, role: str, prompt: str, interval_sec: int) -> int:
        interval = max(MIN_INTERVAL_SEC, int(interval_sec))
        next_run = _iso(_utcnow() + timedelta(seconds=interval))
        cur = await self.db.execute(
            """INSERT INTO autopilots (name, role, prompt, interval_sec, enabled, next_run_at)
               VALUES (?,?,?,?,1,?)""",
            (name.strip()[:120], role, prompt.strip(), interval, next_run),
        )
        return cur.lastrowid

    async def list_all(self) -> list[dict]:
        return await self.db.fetchall("SELECT * FROM autopilots ORDER BY id DESC")

    async def get(self, autopilot_id: int) -> dict | None:
        return await self.db.fetchone("SELECT * FROM autopilots WHERE id=?", (autopilot_id,))

    async def set_enabled(self, autopilot_id: int, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE autopilots SET enabled=? WHERE id=?", (1 if enabled else 0, autopilot_id)
        )

    async def delete(self, autopilot_id: int) -> None:
        await self.db.execute("DELETE FROM autopilots WHERE id=?", (autopilot_id,))

    async def due(self, now: datetime | None = None) -> list[dict]:
        """Autopilot aktif yang sudah waktunya jalan (next_run_at <= sekarang)."""
        now = now or _utcnow()
        return await self.db.fetchall(
            "SELECT * FROM autopilots WHERE enabled=1 AND next_run_at <= ? ORDER BY next_run_at",
            (_iso(now),),
        )

    async def mark_ran(
        self, autopilot_id: int, interval_sec: int, now: datetime | None = None
    ) -> None:
        """Update last_run_at = sekarang, next_run_at = sekarang + interval (misfire-safe)."""
        now = now or _utcnow()
        await self.db.execute(
            "UPDATE autopilots SET last_run_at=?, next_run_at=? WHERE id=?",
            (_iso(now), _iso(now + timedelta(seconds=interval_sec)), autopilot_id),
        )

    async def record_run(
        self,
        autopilot_id: int,
        session_id: str,
        status: str,
        output: str,
        proposals: int,
        error: str = "",
    ) -> None:
        await self.db.execute(
            """INSERT INTO autopilot_runs (autopilot_id, session_id, status, output, proposals, error)
               VALUES (?,?,?,?,?,?)""",
            (autopilot_id, session_id, status, output[:2000], proposals, error[:500]),
        )

    async def recent_runs(self, limit: int = 30) -> list[dict]:
        return await self.db.fetchall(
            """SELECT r.*, a.name AS autopilot_name
               FROM autopilot_runs r LEFT JOIN autopilots a ON a.id = r.autopilot_id
               ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        )


class AutopilotScheduler:
    """Loop asyncio in-process: cek jadwal due tiap tick, jalankan via `runner`.

    Hidup selama server hidup (start di lifespan, stop di shutdown). `runner` adalah
    callable async yang mengeksekusi satu autopilot (mengembalikan jumlah proposal).
    Modul ini tidak tahu soal AgentLoop — web layer menyuntik runner-nya.
    """

    def __init__(
        self,
        store: AutopilotStore,
        runner: AutopilotRunner,
        config: AppConfig = CONFIG,
        tick_sec: int = 30,
    ):
        self.store = store
        self.runner = runner
        self.config = config
        self.tick_sec = tick_sec
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._on_done)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    @staticmethod
    def _on_done(task: asyncio.Task) -> None:
        # Audit #3: error di background task tak boleh hilang diam-diam.
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("autopilot_scheduler_crashed", error=str(exc))

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due_once()
            except Exception as e:  # noqa: BLE001 — satu tick gagal jangan matikan loop
                log.error("autopilot_tick_failed", error=str(e))
            # Tidur sampai tick berikutnya atau diminta berhenti (mana lebih dulu).
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_sec)
            except asyncio.TimeoutError:
                pass

    async def run_due_once(self) -> int:
        """Jalankan semua autopilot yang due sekarang. Return jumlah yang dijalankan.

        Dipisah dari _loop agar bisa di-test langsung tanpa menunggu tick nyata.
        """
        due = await self.store.due()
        for ap in due:
            # Tandai ran SEBELUM eksekusi → jadwalkan ulang lebih dulu agar tick yang
            # tumpang-tindih tidak menjalankan autopilot yang sama dua kali.
            await self.store.mark_ran(ap["id"], ap["interval_sec"])
            await self._run_one(ap)
        return len(due)

    async def _run_one(self, ap: dict) -> None:
        """Eksekusi satu autopilot lewat runner; catat hasil (fail-soft)."""
        try:
            proposals = await self.runner(ap)
            await self.store.record_run(
                ap["id"],
                session_id=f"autopilot-{ap['id']}",
                status="done",
                output="",
                proposals=proposals,
            )
            log.info("autopilot_ran", autopilot=ap["id"], name=ap.get("name"), proposals=proposals)
        except Exception as e:  # noqa: BLE001 — kegagalan run dicatat, tak menjatuhkan scheduler
            await self.store.record_run(
                ap["id"],
                session_id=f"autopilot-{ap['id']}",
                status="error",
                output="",
                proposals=0,
                error=str(e),
            )
            log.error("autopilot_run_failed", autopilot=ap["id"], error=str(e))
