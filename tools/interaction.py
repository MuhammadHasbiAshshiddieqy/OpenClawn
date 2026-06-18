from tools.base import Tool


class AskUserTool(Tool):
    """Klarifikasi ke user.

    Eksekusi nyata ditangani `AgentLoop._execute_tool` lewat `QuestionGate`
    (menunggu jawaban via Future yang di-resolve Web UI). Tool ini menyediakan
    schema agar LLM tahu cara memanggilnya; `execute()` di sini hanya fallback
    bila dijalankan di luar agent loop (mis. test langsung) — bukan jalur utama.
    """

    name = "ask_user"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        # Fallback non-interaktif: jalur utama (interaktif) ada di AgentLoop.
        question = input_data.get("question", "")
        return {"answer": f"[tidak ada UI untuk menjawab: {question}]"}

    def schema(self) -> dict:
        return {
            "name": "ask_user",
            "description": (
                "Tanya klarifikasi ke user saat permintaan ambigu. User akan melihat "
                "pertanyaan di UI dan mengetik jawabannya; jawaban dikembalikan ke kamu. "
                "Gunakan hemat — hanya saat benar-benar perlu, bukan untuk tiap langkah."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        }
