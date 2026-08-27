"""Sandbox image aktif per-sesi (§ Prioritas 8.3, sandbox proyek besar/kompleks).

Sama pola `infra/workspace.py` (`CURRENT_WORKSPACE_ROOT`/`SessionWorkspaceStore`):
`tools/sandbox.py::DockerSandbox` selalu membaca `SANDBOX_IMAGE` dasar via modul
ini, TANPA perlu tahu `session_id` — `ContextVar` diisi `AgentLoop.run()` di awal
turn (dipulihkan dari DB via `SessionSandboxImageStore`, lintas turn maupun
restart server), sama arsitektur dengan folder kerja adaptif. Menghindari
mengubah signature `Tool.execute()` demi ini (alasan sama persis, lihat
docstring modul `infra/workspace.py`).
"""

import contextvars

from infra.database import DatabaseManager

# None → pakai SANDBOX_IMAGE dasar (perilaku lama, tak ada perubahan). Diisi
# AgentLoop.run() dari session_sandbox_image (kalau sesi ini pernah sukses
# menjalankan tool build_sandbox_image, § Prioritas 8.3) sebelum tool loop berjalan.
CURRENT_SANDBOX_IMAGE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "CURRENT_SANDBOX_IMAGE", default=None
)


def effective_sandbox_image(default_image: str) -> str:
    """`CURRENT_SANDBOX_IMAGE` bila diset (image proyek untuk sesi ini, dari
    `build_sandbox_image`), kalau tidak `default_image` (`SANDBOX_IMAGE` dasar)."""
    override = CURRENT_SANDBOX_IMAGE.get()
    return override if override else default_image


class SessionSandboxImageStore:
    """Image sandbox proyek AKTIF per-sesi, tersimpan di DB (bukan cuma
    ContextVar yang mati begitu turn selesai) — pola sama `SessionWorkspaceStore`.
    Satu baris per `session_id` (UPSERT): image aktif SEKARANG, bukan riwayat build.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get(self, session_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT image_tag FROM session_sandbox_image WHERE session_id=?", (session_id,)
        )
        return row["image_tag"] if row else None

    async def set(self, session_id: str, image_tag: str) -> None:
        await self.db.execute(
            """INSERT INTO session_sandbox_image (session_id, image_tag) VALUES (?, ?)
               ON CONFLICT(session_id) DO UPDATE SET image_tag=excluded.image_tag,
               updated_at=CURRENT_TIMESTAMP""",
            (session_id, image_tag),
        )
