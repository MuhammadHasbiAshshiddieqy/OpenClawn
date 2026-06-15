import asyncio
import json
from infra.database import DatabaseManager
from infra.config import AppConfig


class ApprovalGate:
    """
    Human-in-the-loop approval untuk tool destruktif.
    Research phase: auto-approve + log. Sprint 3: ganti ke interaktif via Web UI.
    """

    def __init__(self, db: DatabaseManager, config: AppConfig):
        self.db = db
        self.config = config
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, session_id: str, tool_name: str, tool_input: dict) -> bool:
        await self.db.execute(
            """INSERT INTO approval_log (session_id, tool_name, tool_input, decision)
               VALUES (?,?,?,?)""",
            (session_id, tool_name, json.dumps(tool_input), "approved"),
        )
        # Sprint 3: menunggu Future yang di-resolve oleh Web UI
        return True

    def approve(self, session_id: str, decision: bool) -> None:
        """Dipanggil dari Web UI saat user klik approve/reject."""
        fut = self._pending.pop(session_id, None)
        if fut and not fut.done():
            fut.set_result(decision)
