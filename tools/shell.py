from infra.config import CONFIG
from infra.workspace import (
    WorkspaceViolation,
    effective_workspace_root,
    resolve_in_current_workspace,
)
from tools.base import Tool
from tools.sandbox import DockerSandbox, SandboxUnavailable

MAX_CMD_LEN = 2000


class ShellRunTool(Tool):
    """Jalankan perintah shell read-only DI DALAM Docker sandbox.

    Keamanan #1: TIDAK ada eksekusi di host. Workspace di-mount read-only,
    --network none, non-root, no-new-privileges (lihat DockerSandbox._base_docker_args).
    Bila Docker tidak tersedia → gagal aman, bukan fallback ke host.

    requires_approval=False (§ user request otonomi): sandbox — bukan approval —
    adalah pertahanan di sini (CLAUDE.md §17, "Shield lapisan kosmetik, pertahanan
    utama = container isolation"). Command APA PUN yang dikirim ke sini secara fisik
    tak bisa menulis ke host maupun menjangkau network, jadi meminta approval manusia
    tidak menambah keamanan nyata — hanya gesekan untuk command read-only sehari-hari
    (grep/find/ls/git log). Beda dari code_run (TETAP selalu approval, CLAUDE.md §1 —
    aturan itu tidak disentuh oleh perubahan ini).
    """

    name = "shell_run"
    requires_approval = False

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        command = (input_data.get("command") or "").strip()
        if not command:
            return {"error": "command wajib diisi"}
        if len(command) > MAX_CMD_LEN:
            return {"error": f"command terlalu panjang (maks {MAX_CMD_LEN} karakter)"}
        try:
            return await self.sandbox.run_shell(
                command, effective_workspace_root(CONFIG.workspace_root)
            )
        except SandboxUnavailable as e:
            # Fail-safe: tidak ada Docker → tidak menjalankan apa pun di host.
            return {"error": f"{e}. shell_run butuh Docker dan tidak akan jalan di host."}

    def schema(self) -> dict:
        return {
            "name": "shell_run",
            "description": (
                "Jalankan perintah shell read-only (grep, find, ls, cat, git log, wc) "
                "di dalam sandbox terisolasi atas workspace. Filesystem read-only & tanpa "
                "network — tidak bisa memodifikasi file atau mengakses internet. "
                "Untuk membaca 1 file pakai file_read; untuk list folder pakai list_dir."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Perintah shell read-only. Contoh: 'grep -rn TODO .' atau 'find . -name \"*.py\"'",
                    },
                },
                "required": ["command"],
            },
        }


class ListDirTool(Tool):
    """List isi direktori dalam workspace. Read-only, tidak butuh approval."""

    name = "list_dir"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = (input_data.get("path") or ".").strip()
        try:
            p = resolve_in_current_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}

        if not p.exists():
            return {"error": f"Direktori tidak ditemukan: {path}"}
        if not p.is_dir():
            return {"error": f"Bukan direktori: {path}"}

        try:
            entries = []
            for child in sorted(p.iterdir()):
                entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
            return {"path": str(p), "entries": entries[:200]}  # batasi 200 entri
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        except OSError as e:
            return {"error": f"Gagal membaca direktori: {e}"}

    def schema(self) -> dict:
        return {
            "name": "list_dir",
            "description": (
                "List isi satu direktori (nama file + tipe), dibatasi ke workspace. "
                "Gunakan SEKALI per direktori — jangan panggil ulang dengan path yang sama. "
                "Setelah dapat daftar file, gunakan file_read untuk membaca isinya. "
                "Jika sudah punya daftarnya, langsung jawab tanpa memanggil tool ini lagi."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path direktori relatif terhadap workspace. Default: root workspace.",
                    }
                },
                "required": [],
            },
        }
