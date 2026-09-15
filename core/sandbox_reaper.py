"""Sandbox reaper — siklus hidup sandbox PERSISTEN (§ IMPROVEMENT-Sandbox-
Isolation-Parallelization.md Fase 3, owner disetujui eksplisit).

Bentuk & pola SAMA PERSIS `core/autopilot.py::AutopilotScheduler` (loop asyncio
in-process, `start()`/`stop()` di lifespan, `run_due_once()` terpisah dari
`_loop()` agar bisa di-test tanpa menunggu tick nyata) — dua modul ini
memecahkan masalah yang sama bentuknya ("cek jadwal/keadaan tiap tick, ambil
tindakan"), jadi arsitekturnya seharusnya kembar, bukan diciptakan ulang.

Idle terlalu lama → `docker pause` (hemat CPU host, state tetap ada). Tak
dipakai lebih lama lagi → `docker rm`+`docker volume rm` PERMANEN. Baris DB
dan container Docker sama-sama bertahan lintas restart proses app (SQLite
file-backed + container independen dari proses app) — reaper yang baru start
setelah restart otomatis melanjutkan dari `last_used_at` yang sama, tak ada
yang jadi yatim.
"""

import asyncio
from datetime import datetime, timezone

from infra.config import CONFIG, AppConfig
from infra.database import DatabaseManager
from infra.logging import log
from infra.sandbox_lifecycle import SessionSandboxContainerStore
from tools.sandbox import DockerSandbox


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_sqlite_ts(raw: str) -> datetime:
    """Parse `CURRENT_TIMESTAMP` SQLite ('YYYY-MM-DD HH:MM:SS') sebagai UTC naive."""
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


class SandboxReaper:
    """Loop asyncio in-process: cek `session_sandbox_container` tiap tick,
    pause yang idle, destroy yang lama tak dipakai. Hidup selama server hidup
    (start di lifespan, stop di shutdown) — pola identik `AutopilotScheduler`."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig = CONFIG,
        sandbox: DockerSandbox | None = None,
        tick_sec: int | None = None,
    ):
        self.store = SessionSandboxContainerStore(db)
        self.config = config
        self.sandbox = sandbox or DockerSandbox()
        self.tick_sec = tick_sec if tick_sec is not None else config.sandbox_reaper_tick_sec
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
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("sandbox_reaper_crashed", error=str(exc))

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due_once()
            except Exception as e:  # noqa: BLE001 — satu tick gagal jangan matikan loop
                log.error("sandbox_reaper_tick_failed", error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_sec)
            except asyncio.TimeoutError:
                pass

    async def run_due_once(self, now: datetime | None = None) -> dict:
        """Evaluasi SEMUA sandbox persisten aktif satu kali. Return
        `{"paused": [...], "destroyed": [...]}` (session_id) — dipisah dari
        `_loop` agar test bisa memicu satu evaluasi tanpa menunggu tick nyata,
        sama pola `AutopilotScheduler.run_due_once`.
        """
        now = now or _utcnow()
        paused: list[str] = []
        destroyed: list[str] = []

        for row in await self.store.list_all():
            last_used = _parse_sqlite_ts(row["last_used_at"])
            idle_for = (now - last_used).total_seconds()

            if idle_for > self.config.sandbox_persist_destroy_ttl_sec:
                await self.sandbox.destroy_persistent(row["container_id"], row["volume_name"])
                await self.store.delete(row["session_id"])
                destroyed.append(row["session_id"])
                log.info(
                    "sandbox_persistent_destroyed",
                    session=row["session_id"],
                    idle_for_sec=int(idle_for),
                )
                continue

            if row["state"] == "running" and idle_for > self.config.sandbox_persist_idle_ttl_sec:
                result = await self.sandbox.pause_persistent(row["container_id"])
                if result["ok"]:
                    await self.store.set_state(row["session_id"], "paused")
                    paused.append(row["session_id"])
                    log.info("sandbox_persistent_paused", session=row["session_id"])
                else:
                    log.warning(
                        "sandbox_persistent_pause_failed",
                        session=row["session_id"],
                        error=result["error"],
                    )

        return {"paused": paused, "destroyed": destroyed}
