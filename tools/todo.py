"""Tool todo_write: agent mengelola daftar langkah multi-step yang terlihat user.

Tiap panggilan MENGGANTI seluruh daftar sesi (snapshot, pola sama harness TodoWrite):
agent mengirim list lengkap tiap update, status t.berubah seiring progres. Disimpan
ke tabel agent_todos per session_id; UI bisa menampilkannya agar user melihat rencana.

session_id disuntik AgentLoop sebagai `_session_id` (model tak mengarang sesi).
Read-write ke tabel internal (bukan filesystem) → tidak butuh approval.
"""

from tools.base import Tool

VALID_STATUS = {"pending", "in_progress", "completed"}
MAX_ITEMS = 30


class TodoWriteTool(Tool):
    name = "todo_write"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        session_id = input_data.get("_session_id")
        if not session_id:
            return {"error": "todo_write butuh konteks sesi (internal)"}
        if db is None:
            return {"error": "todo_write butuh database"}
        todos = input_data.get("todos")
        if not isinstance(todos, list) or not todos:
            return {"error": "todos wajib berupa list item (minimal satu)"}
        if len(todos) > MAX_ITEMS:
            return {"error": f"terlalu banyak item (maks {MAX_ITEMS})"}

        # Validasi & normalisasi tiap item sebelum menyentuh DB.
        rows = []
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return {"error": f"item ke-{i} harus objek {{content, status}}"}
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip().lower()
            if not content:
                return {"error": f"item ke-{i} tanpa content"}
            if status not in VALID_STATUS:
                return {"error": f"status '{status}' tak valid (pending/in_progress/completed)"}
            rows.append((session_id, i, content, status))

        # Snapshot: ganti seluruh daftar sesi (hapus lama, tulis baru).
        await db.execute("DELETE FROM agent_todos WHERE session_id=?", (session_id,))
        for r in rows:
            await db.execute(
                "INSERT INTO agent_todos (session_id, position, content, status) VALUES (?,?,?,?)",
                r,
            )
        counts = {s: sum(1 for r in rows if r[3] == s) for s in VALID_STATUS}
        return {"ok": True, "total": len(rows), "counts": counts}

    def schema(self) -> dict:
        return {
            "name": "todo_write",
            "description": (
                "Catat/perbarui daftar langkah rencana kerja (multi-step) agar user "
                "melihat progres. Kirim SELURUH daftar tiap update (snapshot): tiap item "
                "{content, status} dengan status pending|in_progress|completed. "
                "Pakai untuk tugas berlapis; satu item in_progress pada satu waktu."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        }
