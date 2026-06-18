import asyncio
import json
import uuid
from dataclasses import dataclass, field

from infra.database import DatabaseManager
from infra.config import AppConfig
from infra.logging import log


@dataclass
class PendingApproval:
    """Permintaan approval yang menunggu keputusan user dari Web UI."""

    approval_id: str
    session_id: str
    tool_name: str
    tool_input: dict
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class ApprovalGate:
    """
    Human-in-the-loop approval untuk tool destruktif (mis. code_run).

    Interaktif: `request()` membuat Future dan menunggu user
    menekan approve/reject di Web UI (via `resolve()`). Jika user tidak
    merespons dalam approval_timeout_sec → fail-safe DENY (keamanan dulu,
    CLAUDE.md §1.1). Tool destruktif tidak pernah jalan tanpa persetujuan
    eksplisit.
    """

    def __init__(self, db: DatabaseManager, config: AppConfig):
        self.db = db
        self.config = config
        self._pending: dict[str, PendingApproval] = {}

    async def request(self, session_id: str, tool_name: str, tool_input: dict) -> bool:
        approval_id = uuid.uuid4().hex
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        self._pending[approval_id] = pending

        # Catat permintaan dengan status pending — auditor & Web UI butuh ini
        await self.db.execute(
            """INSERT INTO approval_log (session_id, tool_name, tool_input, decision)
               VALUES (?,?,?,?)""",
            (session_id, tool_name, json.dumps(tool_input), f"pending:{approval_id}"),
        )

        try:
            approved = await asyncio.wait_for(
                pending.future, timeout=self.config.approval_timeout_sec
            )
            decision = "approved" if approved else "rejected"
        except asyncio.TimeoutError:
            # Fail-safe: tidak ada respons → tolak. code_run tidak boleh jalan diam-diam.
            approved = False
            decision = "timeout"
            log.warning(
                "approval_timeout", session=session_id, tool=tool_name, approval_id=approval_id
            )
        finally:
            self._pending.pop(approval_id, None)

        await self._record_decision(approval_id, decision)
        return approved

    async def _record_decision(self, approval_id: str, decision: str) -> None:
        """Update baris pending menjadi keputusan final."""
        await self.db.execute(
            "UPDATE approval_log SET decision=? WHERE decision=?",
            (decision, f"pending:{approval_id}"),
        )

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """
        Dipanggil dari Web UI saat user klik approve/reject.
        Return True jika approval_id valid dan berhasil di-resolve.
        """
        pending = self._pending.get(approval_id)
        if pending and not pending.future.done():
            pending.future.set_result(approved)
            return True
        return False

    def pending_list(self, session_id: str | None = None) -> list[dict]:
        """Daftar approval yang masih menunggu — untuk ditampilkan di Web UI."""
        return [
            {
                "approval_id": p.approval_id,
                "session_id": p.session_id,
                "tool_name": p.tool_name,
                "tool_input": p.tool_input,
            }
            for p in self._pending.values()
            if session_id is None or p.session_id == session_id
        ]
