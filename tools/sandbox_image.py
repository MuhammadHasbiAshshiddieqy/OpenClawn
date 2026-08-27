"""Tool `build_sandbox_image` — bangun image sandbox khusus SATU proyek dengan
dependency Python di-*bake* saat `docker build` (§ Prioritas 8.3, keputusan
owner: opsi (a) — image kustom per-proyek, network hanya terbuka saat build,
BUKAN saat eksekusi kode sungguhan via code_run/shell_run).

Setelah tool ini sukses, `code_run`/`shell_run` OTOMATIS memakai image baru
untuk SISA sesi ini (`infra/sandbox_image.py::CURRENT_SANDBOX_IMAGE`,
dipulihkan lintas turn/restart dari `session_sandbox_image` — pola sama
folder kerja adaptif) — model TIDAK perlu memanggil tool lain untuk
"memilih" image.
"""

from infra.config import CONFIG
from infra.sandbox_image import SessionSandboxImageStore
from infra.workspace import WorkspaceViolation, resolve_in_current_workspace
from tools.base import Tool
from tools.sandbox import DockerSandbox, SandboxUnavailable

MAX_REQUIREMENTS_BYTES = 20_000
MAX_REQUIREMENTS_LINES = 200


def _validate_requirements(content: str) -> str | None:
    """Validasi ringan `requirements.txt` SEBELUM dipakai membangun image
    dengan network terbuka (§ residual risk, lihat docstring
    `DockerSandbox.build_project_image`). Return pesan error atau `None` bila lolos.

    Menolak baris yang diawali `-` (opsi pip: `-e`/`--index-url`/`-r`/`-c`/dst)
    — ini satu-satunya cara `requirements.txt` bisa mengalihkan sumber paket ke
    index pihak ketiga tak tepercaya atau instalasi VCS/lokal arbitrer. Paket
    dari PyPI resmi via nama biasa (`paket==1.2.3`) tetap bisa menjalankan kode
    arbitrer lewat `setup.py`/build backend-nya sendiri saat `pip install` —
    risiko inheren pip, TIDAK dihapus validasi ini, hanya dipersempit permukaannya.
    """
    if not content.strip():
        return "requirements.txt kosong — tidak ada yang perlu dibangun"
    if len(content.encode()) > MAX_REQUIREMENTS_BYTES:
        return f"requirements.txt melebihi {MAX_REQUIREMENTS_BYTES} byte"
    lines = content.splitlines()
    if len(lines) > MAX_REQUIREMENTS_LINES:
        return f"requirements.txt melebihi {MAX_REQUIREMENTS_LINES} baris"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            return f"baris opsi pip ditolak (tak diizinkan): '{stripped}'"
    return None


class BuildSandboxImageTool(Tool):
    """Bangun image sandbox proyek dari `requirements.txt` di workspace.

    SELALU butuh approval (CLAUDE.md §1, non-negotiable) — build-nya sendiri
    membuka network sementara, sama kelas sensitivitas dengan `code_run`
    (lihat `AgentLoop._TRUST_MODE_EXEMPT`, tak bisa dilewati trust mode).
    """

    name = "build_sandbox_image"
    requires_approval = True

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = (input_data.get("path") or "requirements.txt").strip()
        session_id = input_data.get("_session_id")
        try:
            safe = resolve_in_current_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        try:
            content = safe.read_text()
        except FileNotFoundError:
            return {"error": f"'{path}' tidak ditemukan di workspace"}
        except IsADirectoryError:
            return {"error": f"'{path}' adalah direktori, bukan file"}

        validation_err = _validate_requirements(content)
        if validation_err:
            return {"error": validation_err}

        try:
            result = await self.sandbox.build_project_image(content)
        except SandboxUnavailable as e:
            return {"error": f"{e}. build_sandbox_image butuh Docker."}

        if result["ok"] and db is not None and session_id:
            await SessionSandboxImageStore(db).set(session_id, result["image"])

        return result

    def schema(self) -> dict:
        return {
            "name": "build_sandbox_image",
            "description": (
                "Bangun image sandbox KHUSUS proyek ini dari requirements.txt di "
                "workspace — dependency di-install SEKALI SAAT BUILD (network hanya "
                "terbuka di situ), lalu code_run/shell_run otomatis memakai image ini "
                "untuk SISA sesi (tanpa network saat eksekusi, seperti biasa). "
                "Pakai ini SEBELUM code_run bila proyek butuh paket di luar numpy/pandas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path requirements.txt relatif ke workspace. "
                            "Default 'requirements.txt'."
                        ),
                    },
                },
                "required": [],
            },
        }
