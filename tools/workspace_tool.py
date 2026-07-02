from infra.workspace import (
    CURRENT_WORKSPACE_ROOT,
    SessionWorkspaceStore,
    validate_workdir_candidate,
)
from tools.base import Tool


class SetWorkdirTool(Tool):
    """Pindahkan folder kerja aktif untuk SISA sesi ini (§ user request: "pindah
    direktori secara dinamis" lewat chat — sebelumnya folder kerja HANYA bisa
    diubah lewat field UI sekali per-request, tak ada cara mengubahnya di
    tengah percakapan).

    Efek ganda saat sukses:
    1. `CURRENT_WORKSPACE_ROOT` (ContextVar) di-set LANGSUNG — tool file/shell/git
       berikutnya di TURN INI juga ikut pindah, tanpa menunggu turn baru.
    2. Ditulis ke `session_workspace` (DB) — turn BERIKUTNYA (AgentLoop baru per
       request web) memuatnya balik sebagai folder aktif, jadi perpindahan
       bertahan sepanjang sesi, bukan cuma turn ini.

    `_session_id` disuntik AgentLoop._execute_tool (sama pola todo_write/
    report_blocker) — model tak boleh & tak perlu mengarang session_id sendiri.
    Read-only-effect di luar workspace (tak menulis file/jalankan kode) →
    requires_approval=False, sama alasan shell_run (§ CLAUDE.md §17, sandbox/
    validasi path adalah pertahanan, bukan approval).
    """

    name = "set_workdir"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = (input_data.get("path") or "").strip()
        session_id = input_data.get("_session_id")
        if not path:
            return {"error": "path wajib diisi"}
        if not session_id:
            return {"error": "set_workdir butuh konteks sesi (internal)"}

        resolved, err = validate_workdir_candidate(path)
        if err:
            return {"error": err}

        CURRENT_WORKSPACE_ROOT.set(resolved)
        if db is not None:
            await SessionWorkspaceStore(db).set(session_id, resolved)

        return {"ok": True, "workdir": resolved}

    def schema(self) -> dict:
        return {
            "name": "set_workdir",
            "description": (
                "Pindahkan folder kerja aktif untuk sisa sesi chat ini. Semua tool "
                "file/shell/git berikutnya (di turn ini DAN turn selanjutnya dalam sesi "
                "yang sama) memakai folder baru ini sampai dipindah lagi. Path harus "
                "berupa direktori yang benar-benar ada di mesin. Gunakan ini saat user "
                "secara eksplisit meminta pindah folder kerja (mis. 'pindah ke folder X', "
                "'kerja di ~/project-y sekarang')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path direktori tujuan (absolut atau relatif ke home user, mis. '~/project-y').",
                    },
                },
                "required": ["path"],
            },
        }
