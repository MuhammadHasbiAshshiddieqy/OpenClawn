"""Tool `sandbox_persist_enable` — hidupkan sandbox PERSISTEN untuk sesi ini
(§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 3, sandbox lifecycle —
owner disetujui EKSPLISIT setelah trade-off keamanan dijelaskan lewat
`AskUserQuestion`, bukan inisiatif agent).

Trade-off yang diterima owner, JUJUR dicatat di sini (§1/§17 — jangan beri
rasa aman palsu): `/work` jadi writable+persisten per sesi lewat named Docker
volume, menggantikan mount temp-dir read-only sekali-pakai. Kode berbahaya di
satu panggilan `code_run` BISA meninggalkan jejak yang bertahan sampai
container di-recycle `core/sandbox_reaper.py`. `--network none`, non-root,
`no-new-privileges` TETAP tak berubah — HANYA persistensi filesystem yang
dilonggarkan, tak ada lagi.

Setelah tool ini sukses, `code_run` (HANYA `code_run` — TIDAK `shell_run`,
lihat `infra/sandbox_lifecycle.py`) OTOMATIS exec ke container yang sama untuk
SISA sesi ini (`CURRENT_PERSISTENT_SANDBOX`, dipulihkan lintas turn/restart
dari `session_sandbox_container` — pola sama `build_sandbox_image`/
`CURRENT_SANDBOX_IMAGE`) — model tidak perlu memanggil tool lain untuk
"memakai" sandbox persisten setelah mengaktifkannya.
"""

from infra.config import CONFIG
from infra.sandbox_lifecycle import SessionSandboxContainerStore
from tools.base import Tool
from tools.sandbox import DockerSandbox, SandboxUnavailable


class SandboxPersistEnableTool(Tool):
    """Aktifkan sandbox persisten untuk sesi ini.

    SELALU butuh approval (CLAUDE.md §1, non-negotiable) — SEKALIGUS lebih
    sensitif dari `build_sandbox_image`: membuat state WRITABLE yang bertahan
    lintas panggilan, bukan cuma network sesaat saat build (lihat
    `AgentLoop._TRUST_MODE_EXEMPT`, tak bisa dilewati trust mode).
    """

    name = "sandbox_persist_enable"
    requires_approval = True

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        session_id = input_data.get("_session_id")
        if not session_id:
            return {"error": "sandbox_persist_enable butuh konteks sesi (internal)"}
        if db is None:
            return {"error": "sandbox_persist_enable butuh database"}

        store = SessionSandboxContainerStore(db)

        # Idempoten: sesi ini sudah punya sandbox persisten aktif → no-op sukses,
        # jangan buat container kedua untuk sesi yang sama.
        existing = await store.get(session_id)
        if existing is not None:
            return {
                "ok": True,
                "already_enabled": True,
                "container_id": existing["container_id"],
            }

        active = await store.count_active()
        if active >= CONFIG.sandbox_persist_max_containers:
            return {
                "error": (
                    f"Batas sandbox persisten aktif tercapai ({active}/"
                    f"{CONFIG.sandbox_persist_max_containers}). Tunggu salah satu "
                    "di-recycle karena idle, atau lanjutkan tanpa sandbox persisten."
                )
            }

        try:
            result = await self.sandbox.create_persistent(session_id)
        except SandboxUnavailable as e:
            return {"error": f"{e}. sandbox_persist_enable butuh Docker."}

        if not result["ok"]:
            return {"error": f"Gagal membuat sandbox persisten: {result['error']}"}

        await store.create(session_id, result["container_id"], result["volume_name"])
        return {"ok": True, "already_enabled": False, "container_id": result["container_id"]}

    def schema(self) -> dict:
        return {
            "name": "sandbox_persist_enable",
            "description": (
                "Hidupkan sandbox PERSISTEN untuk sesi ini — file & package yang "
                "diinstall lewat code_run akan BERTAHAN antar panggilan code_run "
                "berikutnya (tidak lagi dihapus tiap panggilan). Container di-pause "
                "otomatis saat idle, di-recycle setelah lama tak dipakai. Pakai HANYA "
                "bila proyek benar-benar butuh iterasi kode berulang dengan state yang "
                "harus bertahan (mis. install package sekali, pakai berkali-kali) — "
                "jangan pakai untuk eksekusi kode sekali-jalan biasa."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }
