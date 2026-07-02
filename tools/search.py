"""Tool pencarian: glob (cari file by pattern) + grep (cari teks di file).

Keduanya pure-Python & dibatasi ke workspace — tidak butuh shell, tidak menyentuh
host, lebih mudah dipanggil model lokal ketimbang menyusun perintah `find`/`grep`.
"""

import re

from infra.config import CONFIG
from infra.workspace import WorkspaceViolation, resolve_in_current_workspace
from tools.base import Tool

MAX_GLOB_RESULTS = 200
MAX_GREP_MATCHES = 100
# Folder yang dilewati saat scan rekursif — hemat waktu & hindari noise.
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def _iter_files(root, rel_dir):
    """Yield file di dalam root, melewati SKIP_DIRS. rel_dir membatasi subtree."""
    base = root / rel_dir if rel_dir else root
    if not base.exists():
        return
    for p in base.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


class GlobTool(Tool):
    name = "glob"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        pattern = (input_data.get("pattern") or "").strip()
        sub = (input_data.get("path") or "").strip()
        if not pattern:
            return {"error": "pattern wajib diisi (mis. '*.py' atau '**/test_*.py')"}
        try:
            root = resolve_in_current_workspace(".", CONFIG.workspace_root)
            base = resolve_in_current_workspace(sub, CONFIG.workspace_root) if sub else root
        except WorkspaceViolation as e:
            return {"error": str(e)}

        try:
            matches = []
            for p in base.glob(pattern):
                if any(part in SKIP_DIRS for part in p.parts):
                    continue
                if p.is_file():
                    matches.append(str(p.relative_to(root)))
                    if len(matches) >= MAX_GLOB_RESULTS:
                        break
            return {"matches": sorted(matches), "count": len(matches)}
        except (OSError, ValueError) as e:
            return {"error": f"Glob gagal: {e}"}

    def schema(self) -> dict:
        return {
            "name": "glob",
            "description": (
                "Cari file berdasarkan pola nama (glob) di dalam workspace. "
                "Contoh pattern: '*.py' (di root), '**/*.py' (rekursif), 'src/**/test_*.py'. "
                "Pakai ini untuk menemukan file sebelum membacanya dengan file_read."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Pola glob, mis. '**/*.py'"},
                    "path": {
                        "type": "string",
                        "description": "Subfolder awal (opsional, default seluruh workspace).",
                    },
                },
                "required": ["pattern"],
            },
        }


class GrepTool(Tool):
    name = "grep"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        pattern = input_data.get("pattern") or ""
        sub = (input_data.get("path") or "").strip()
        if not pattern:
            return {"error": "pattern (regex) wajib diisi"}
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return {"error": f"Regex tidak valid: {e}"}
        try:
            root = resolve_in_current_workspace(".", CONFIG.workspace_root)
            _ = resolve_in_current_workspace(sub, CONFIG.workspace_root) if sub else root
        except WorkspaceViolation as e:
            return {"error": str(e)}

        matches: list[dict] = []
        for p in _iter_files(root, sub):
            try:
                with open(p, encoding="utf-8", errors="strict") as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append(
                                {
                                    "file": str(p.relative_to(root)),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:300],
                                }
                            )
                            if len(matches) >= MAX_GREP_MATCHES:
                                return {
                                    "matches": matches,
                                    "count": len(matches),
                                    "truncated": True,
                                }
            except (UnicodeDecodeError, PermissionError, OSError):
                continue  # lewati file binary/tak terbaca
        return {"matches": matches, "count": len(matches), "truncated": False}

    def schema(self) -> dict:
        return {
            "name": "grep",
            "description": (
                "Cari teks/pola (regex) di dalam isi file pada workspace. "
                "Mengembalikan file, nomor baris, dan teks yang cocok. "
                "Pakai ini untuk menemukan di mana sebuah fungsi/variabel/string didefinisikan."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Pola regex yang dicari."},
                    "path": {
                        "type": "string",
                        "description": "Subfolder pencarian (opsional, default seluruh workspace).",
                    },
                },
                "required": ["pattern"],
            },
        }
