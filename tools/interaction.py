from tools.base import Tool


class AskUserTool(Tool):
    """Stub untuk interaksi dengan user. Sprint 3 diganti dengan SSE event ke Web UI."""

    name = "ask_user"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        question = input_data.get("question", "")
        # Sprint 3: kirim event ke Web UI dan tunggu jawaban
        return {"answer": f"[stub] pertanyaan tertunda: {question}"}

    def schema(self) -> dict:
        return {
            "name": "ask_user",
            "description": "Tanya klarifikasi ke user",
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        }
