"""Tool dokumen: pdf_read — ekstrak teks dari PDF dalam workspace.

pypdf murni-Python (pengecualian dependency yang disetujui owner, lihat CLAUDE.md §7).
"""

from pypdf import PdfReader

from infra.config import CONFIG
from infra.workspace import WorkspaceViolation, resolve_in_workspace
from tools.base import Tool

MAX_PDF_CHARS = CONFIG.tool_max_output


class PdfReadTool(Tool):
    name = "pdf_read"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = (input_data.get("path") or "").strip()
        if not path:
            return {"error": "path wajib diisi"}
        try:
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        if not safe.exists():
            return {"error": f"File tidak ditemukan: {path}"}

        try:
            reader = PdfReader(str(safe))
        except Exception as e:  # pypdf melempar beragam error parsing — tangani semua
            return {"error": f"Gagal membuka PDF: {e}"}

        # Halaman opsional: 1-indexed agar natural bagi user. Default: semua.
        page_arg = input_data.get("page")
        total = len(reader.pages)
        try:
            if page_arg is not None:
                idx = int(page_arg) - 1
                if idx < 0 or idx >= total:
                    return {"error": f"Halaman {page_arg} di luar rentang (1..{total})"}
                pages = [reader.pages[idx]]
            else:
                pages = reader.pages
            text = "\n".join(p.extract_text() or "" for p in pages)
        except Exception as e:
            return {"error": f"Gagal ekstrak teks: {e}"}

        return {
            "pages": total,
            "text": text[:MAX_PDF_CHARS],
            "truncated": len(text) > MAX_PDF_CHARS,
        }

    def schema(self) -> dict:
        return {
            "name": "pdf_read",
            "description": (
                "Ekstrak teks dari file PDF dalam workspace. "
                "Opsional 'page' (1-indexed) untuk satu halaman saja. "
                "Pakai untuk membaca dokumen/laporan/spec berformat PDF."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path PDF relatif ke workspace."},
                    "page": {
                        "type": "integer",
                        "description": "Nomor halaman (1-indexed). Kosong = semua.",
                    },
                },
                "required": ["path"],
            },
        }
