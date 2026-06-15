from tools.base import Tool
from tools.sandbox import DockerSandbox


class CodeRunTool(Tool):
    name = "code_run"
    requires_approval = True  # selalu butuh approval — eksekusi kode

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def execute(self, input_data: dict, vault) -> dict:
        code = input_data.get("code", "")
        if not code:
            return {"error": "Tidak ada kode untuk dijalankan"}
        return await self.sandbox.run_python(code)

    def schema(self) -> dict:
        return {
            "name": "code_run",
            "description": "Jalankan kode Python dalam sandbox terisolasi (no network)",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        }
