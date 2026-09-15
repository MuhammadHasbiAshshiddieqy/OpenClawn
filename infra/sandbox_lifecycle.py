"""Sandbox persisten aktif per-sesi (§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md
Fase 3 — sandbox lifecycle, dipilih owner secara eksplisit setelah trade-off
keamanan dijelaskan: `/work` jadi writable+persisten per sesi lewat named Docker
volume, bukan lagi mount temp-dir read-only sekali-pakai. `--network none`,
non-root, `no-new-privileges` TETAP tak berubah — lihat `tools/sandbox.py`.

Sama pola `infra/sandbox_image.py` (`CURRENT_SANDBOX_IMAGE`/
`SessionSandboxImageStore`): `tools/sandbox.py::DockerSandbox.run_python`
membaca `effective_persistent_container()` TANPA perlu tahu `session_id` —
`ContextVar` diisi `AgentLoop.run()` di awal turn (dipulihkan dari DB via
`SessionSandboxContainerStore`, lintas turn maupun restart server — container
Docker & baris SQLite sama-sama bertahan lintas restart proses app, tak ada
yang jadi yatim). Menghindari mengubah signature `Tool.execute()` demi ini,
alasan sama persis `infra/workspace.py`.

Lingkup SENGAJA hanya `code_run` (`run_python`) — TIDAK PERNAH `shell_run`.
`shell_run` ada untuk inspeksi WORKSPACE ASLI read-only (grep/find/git log);
`code_run` sama sekali tak pernah mount workspace asli (kode ditulis ke
temp-dir sekali pakai hari ini) — mencampur persistence ke `shell_run` akan
mencampur dua concern yang tak berhubungan tanpa manfaat.
"""

import contextvars

from infra.database import DatabaseManager

# None → mode ephemeral (perilaku lama, tak ada perubahan). Diisi AgentLoop.run()
# dari session_sandbox_container (kalau sesi ini pernah sukses menjalankan tool
# sandbox_persist_enable) sebelum tool loop berjalan.
CURRENT_PERSISTENT_SANDBOX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "CURRENT_PERSISTENT_SANDBOX", default=None
)


def effective_persistent_container() -> str | None:
    """`container_id` aktif untuk sesi ini, atau `None` (mode ephemeral,
    `docker run --rm` sekali-pakai seperti biasa)."""
    return CURRENT_PERSISTENT_SANDBOX.get()


class SessionSandboxContainerStore:
    """Container sandbox persisten AKTIF per-sesi, tersimpan di DB — pola sama
    `SessionSandboxImageStore`. Satu baris per `session_id`: container yang
    SEDANG hidup untuk sesi itu, bukan riwayat. State operasional murni (bukan
    audit trail) — baris DIHAPUS begitu container di-destroy, tak disimpan
    sebagai `state='destroyed'`, tak ada yang perlu ditelusuri setelahnya.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get(self, session_id: str) -> dict | None:
        return await self.db.fetchone(
            """SELECT session_id, container_id, volume_name, state, last_used_at
               FROM session_sandbox_container WHERE session_id=?""",
            (session_id,),
        )

    async def create(self, session_id: str, container_id: str, volume_name: str) -> None:
        await self.db.execute(
            """INSERT INTO session_sandbox_container (session_id, container_id, volume_name)
               VALUES (?, ?, ?)""",
            (session_id, container_id, volume_name),
        )

    async def touch(self, session_id: str) -> None:
        """Perbarui `last_used_at` — dipanggil tiap kali container ini benar-benar
        dipakai, agar reaper (`core/sandbox_reaper.py`) tahu ini bukan idle."""
        await self.db.execute(
            "UPDATE session_sandbox_container SET last_used_at=CURRENT_TIMESTAMP WHERE session_id=?",
            (session_id,),
        )

    async def set_state(self, session_id: str, state: str) -> None:
        await self.db.execute(
            "UPDATE session_sandbox_container SET state=? WHERE session_id=?",
            (state, session_id),
        )

    async def delete(self, session_id: str) -> None:
        await self.db.execute(
            "DELETE FROM session_sandbox_container WHERE session_id=?", (session_id,)
        )

    async def list_all(self) -> list[dict]:
        """Semua container aktif — dipakai reaper mengevaluasi idle/destroy TTL."""
        return await self.db.fetchall(
            "SELECT session_id, container_id, volume_name, state, last_used_at "
            "FROM session_sandbox_container"
        )

    async def count_active(self) -> int:
        """Jumlah sandbox persisten aktif SEKARANG — dicek `sandbox_persist_enable`
        terhadap `AppConfig.sandbox_persist_max_containers` SEBELUM membuat yang
        baru. Batas ini menutup permukaan DoS baru yang tak ada di model ephemeral
        (banyak sesi opt-in sekaligus bisa membebani host tanpa batas)."""
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM session_sandbox_container")
        return row["n"] if row else 0
