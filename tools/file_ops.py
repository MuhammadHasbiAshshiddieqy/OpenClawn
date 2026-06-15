import aiofiles
from tools.base import Tool


class FileReadTool(Tool):
    name = "file_read"
    requires_approval = False

    async def execute(self, input_data: dict, vault) -> dict:
        path = input_data.get("path", "")
        if not path:
            return {"error": "path wajib diisi"}
        try:
            async with aiofiles.open(path) as f:
                content = await f.read()
            return {"content": content[:10000]}
        except FileNotFoundError:
            return {"error": f"File tidak ditemukan: {path}"}
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}

    def schema(self) -> dict:
        return {
            "name": "file_read",
            "description": "Baca isi file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }


class FileWriteTool(Tool):
    name = "file_write"
    requires_approval = False

    async def execute(self, input_data: dict, vault) -> dict:
        path = input_data.get("path", "")
        content = input_data.get("content", "")
        if not path:
            return {"error": "path wajib diisi"}
        try:
            async with aiofiles.open(path, "w") as f:
                await f.write(content)
            return {"ok": True, "path": path}
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}

    def schema(self) -> dict:
        return {
            "name": "file_write",
            "description": "Tulis konten ke file",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }
