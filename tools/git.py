"""Git read-only tools: git_status, git_diff, git_log.

Keamanan #1: TIDAK ada eksekusi di host. Semua git command dijalankan DI DALAM
DockerSandbox (workspace read-only, --network none, non-root) lewat run_shell —
sama seperti shell_run. Bedanya: command di sini KONSTAN & read-only (status/diff/
log), jadi tidak butuh approval. Argumen user dibatasi ketat (path/jumlah) untuk
mencegah injeksi opsi git arbitrer.
"""

import shlex

from infra.config import CONFIG
from infra.workspace import effective_workspace_root
from tools.base import Tool
from tools.sandbox import DockerSandbox, SandboxUnavailable

MAX_LOG_COUNT = 50
DEFAULT_LOG_COUNT = 15


class _GitToolBase(Tool):
    """Basis git read-only: bangun command aman, jalankan di sandbox, fail-safe."""

    requires_approval = False  # read-only, tidak memodifikasi apa pun

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def _run(self, git_args: str) -> dict:
        # `git -C /work` memastikan operasi di workspace yang dimount read-only.
        command = f"git -C /work {git_args}"
        try:
            result = await self.sandbox.run_shell(
                command, effective_workspace_root(CONFIG.workspace_root)
            )
        except SandboxUnavailable as e:
            return {"error": f"{e}. Git tools butuh Docker; tidak jalan di host."}
        # Workspace tanpa .git → git keluar non-zero; ubah jadi pesan jelas.
        if result.get("exit_code") not in (0, None) and "not a git repository" in (
            result.get("stderr", "").lower()
        ):
            return {"error": "Workspace bukan repository git (tidak ada .git)."}
        return result


class GitStatusTool(_GitToolBase):
    name = "git_status"

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        # --porcelain: output ringkas & stabil untuk dibaca model.
        return await self._run("status --porcelain=v1 --branch")

    def schema(self) -> dict:
        return {
            "name": "git_status",
            "description": (
                "Lihat status git workspace (file termodifikasi/untracked + branch) "
                "dalam format porcelain. Read-only, tanpa approval."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        }


class GitDiffTool(_GitToolBase):
    name = "git_diff"

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = (input_data.get("path") or "").strip()
        staged = bool(input_data.get("staged"))
        args = "diff --stat" if not input_data.get("full") else "diff"
        if staged:
            args += " --cached"
        if path:
            # shlex.quote mencegah injeksi opsi/perintah lewat path.
            args += f" -- {shlex.quote(path)}"
        return await self._run(args)

    def schema(self) -> dict:
        return {
            "name": "git_diff",
            "description": (
                "Lihat perubahan git workspace. Default ringkas (--stat); set full=true "
                "untuk diff penuh, staged=true untuk perubahan ter-stage, path untuk "
                "membatasi ke satu file. Read-only, tanpa approval."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Batasi ke satu path (opsional)."},
                    "staged": {"type": "boolean", "description": "Perubahan ter-stage saja."},
                    "full": {"type": "boolean", "description": "Diff penuh, bukan --stat."},
                },
                "required": [],
            },
        }


class GitLogTool(_GitToolBase):
    name = "git_log"

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        try:
            count = int(input_data.get("count") or DEFAULT_LOG_COUNT)
        except (ValueError, TypeError):
            count = DEFAULT_LOG_COUNT
        count = max(1, min(MAX_LOG_COUNT, count))  # jepit agar output tak membanjiri
        # Format satu baris per commit: hash pendek, subjek, author rel-date.
        return await self._run(f"log -n {count} --pretty=format:'%h %s (%an, %ar)'")

    def schema(self) -> dict:
        return {
            "name": "git_log",
            "description": (
                f"Lihat riwayat commit terbaru (default {DEFAULT_LOG_COUNT}, maks "
                f"{MAX_LOG_COUNT}): hash, subjek, author, waktu relatif. Pakai untuk "
                "menelusuri kapan sesuatu berubah / mencari regresi. Read-only, tanpa approval."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Jumlah commit (1..50)."},
                },
                "required": [],
            },
        }
