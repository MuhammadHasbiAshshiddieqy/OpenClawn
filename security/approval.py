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
    # Audit produksi 2026-07-29: SEBELUMNYA tak ada kepemilikan sama sekali —
    # user manapun yang login (termasuk role rendah) bisa lihat & approve/reject
    # approval milik user lain via GET /approvals (tanpa session_id) + POST
    # /approve (approval_id apa pun, tanpa cek kepemilikan). None = tak ada
    # owner tercatat (auth nonaktif, atau caller tak menyertakan) — tetap
    # terlihat semua (graceful, bukan fail-closed total untuk kasus ini),
    # enforcement ownership sungguhan ada di web/main.py (lapisan endpoint).
    owner_user_id: str | None = None
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

    async def request(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        approval_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """`approval_id` opsional — caller (AgentLoop) bisa pre-generate & emit ke UI
        SEBELUM memanggil ini, agar user tahu ID-nya sementara request() masih menunggu
        (§ chat approval UI). Default None → generate seperti sebelumnya (tak ada
        perubahan perilaku untuk caller lama).

        `owner_user_id` (audit produksi 2026-07-29): identitas user yang memicu
        approval ini, dipakai web/main.py untuk menggerbangi GET /approvals dan
        POST /approve agar user lain tak bisa lihat/putuskan approval ini.
        None (default) bila auth nonaktif atau caller tak menyertakan."""
        approval_id = approval_id or uuid.uuid4().hex
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            owner_user_id=owner_user_id,
        )
        self._pending[approval_id] = pending

        # Catat permintaan dengan status pending — auditor & Web UI butuh ini.
        # approval_id di kolom SENDIRI (bukan encode di 'decision') agar tetap
        # query-able setelah keputusan final ditulis (§ Human Approval Pipeline,
        # TODO.md Prioritas 2) — GET /approval/{approval_id} bisa melacak satu
        # approval lintas status pending→approved/rejected/timeout.
        await self.db.execute(
            """INSERT INTO approval_log
               (session_id, tool_name, tool_input, decision, approval_id, owner_user_id)
               VALUES (?,?,?,?,?,?)""",
            (session_id, tool_name, json.dumps(tool_input), "pending", approval_id, owner_user_id),
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
        """Update baris pending menjadi keputusan final, dicari via kolom approval_id
        (bukan lagi via 'decision=pending:{id}' yang rapuh — lihat request())."""
        await self.db.execute(
            "UPDATE approval_log SET decision=? WHERE approval_id=? AND decision='pending'",
            (decision, approval_id),
        )

    async def auto_approve(self, session_id: str, tool_name: str, tool_input: dict) -> bool:
        """Setuju otomatis untuk "Trust mode" per-sesi (§ user request otonomi).

        BEDA dari `queue_proposal` (autopilot — tanpa manusia, TIDAK dieksekusi,
        jadi proposal): trust mode berarti manusia SEDANG hadir di sesi chat aktif
        dan secara sadar memilih melewati klik Approve — tool tetap DIEKSEKUSI
        sungguhan. Tetap tercatat di approval_log (decision="auto:trust_mode",
        bukan "approved" biasa) agar audit trail membedakan keputusan manual vs
        toggle trust mode. Selalu return True — caller (AgentLoop) yang memutuskan
        tool mana yang boleh lewat sini (code_run TIDAK PERNAH, CLAUDE.md §1).
        """
        await self.db.execute(
            """INSERT INTO approval_log (session_id, tool_name, tool_input, decision)
               VALUES (?,?,?,?)""",
            (session_id, tool_name, json.dumps(tool_input), "auto:trust_mode"),
        )
        return True

    async def queue_proposal(self, session_id: str, tool_name: str, tool_input: dict) -> None:
        """Antri aksi destruktif dari autopilot sebagai PROPOSAL (tanpa Future hidup).

        Berbeda dari `request()`: tidak ada manusia menunggu, jadi tidak ada Future &
        tidak memblokir. Hanya mencatat baris pending bertanda `proposal:` di
        approval_log agar user bisa meninjau nanti. Eksekusi nyata TIDAK terjadi di
        sini — keputusan tetap di tangan user (CLAUDE.md §17). Fail-soft: kegagalan
        tulis hanya di-log, tidak menjatuhkan run autopilot.
        """
        try:
            await self.db.execute(
                """INSERT INTO approval_log (session_id, tool_name, tool_input, decision)
                   VALUES (?,?,?,?)""",
                (session_id, tool_name, json.dumps(tool_input), "proposal:pending"),
            )
        except Exception as e:  # noqa: BLE001 — antrian proposal bukan jalur kritis
            log.error("proposal_queue_failed", session=session_id, tool=tool_name, error=str(e))

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """
        Dipanggil dari Web UI saat user klik approve/reject.
        Return True jika approval_id valid dan berhasil di-resolve.

        Cek kepemilikan TIDAK dilakukan di sini secara sengaja — dilakukan
        caller (web/main.py, via `find_pending`) SEBELUM memanggil ini, sama
        pola dengan `_require_role` yang digerbangi di lapisan endpoint,
        bukan di service layer. `resolve()` tetap mekanisme murni.
        """
        pending = self._pending.get(approval_id)
        if pending and not pending.future.done():
            pending.future.set_result(approved)
            return True
        return False

    def find_pending(self, approval_id: str) -> PendingApproval | None:
        """Cari satu pending approval by ID — dipakai web/main.py untuk cek
        kepemilikan (`owner_user_id`) SEBELUM memanggil `resolve()`."""
        return self._pending.get(approval_id)

    def pending_list(
        self, session_id: str | None = None, owner_user_id: str | None = None
    ) -> list[dict]:
        """Daftar approval yang masih menunggu — untuk ditampilkan di Web UI.

        `owner_user_id` (audit produksi 2026-07-29): bila diisi, hanya
        kembalikan approval milik user ini ATAU approval tanpa owner tercatat
        (`None` — dibuat saat auth nonaktif, tetap terlihat semua secara
        graceful). None (default, dipakai admin/auth nonaktif) = tanpa filter
        kepemilikan sama sekali."""
        return [
            {
                "approval_id": p.approval_id,
                "session_id": p.session_id,
                "tool_name": p.tool_name,
                "tool_input": p.tool_input,
            }
            for p in self._pending.values()
            if (session_id is None or p.session_id == session_id)
            and (
                owner_user_id is None or p.owner_user_id is None or p.owner_user_id == owner_user_id
            )
        ]
