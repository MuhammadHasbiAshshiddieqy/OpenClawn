import asyncio
import json
import uuid
from dataclasses import dataclass, field

from core.audit_chain import (
    ENTRY_APPROVAL_AUTO,
    ENTRY_APPROVAL_DECIDED,
    ENTRY_APPROVAL_REQUESTED,
    AuditChain,
)
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
    # Non-Human Identity (§ Prioritas 9.2): "{role}@{hash12}" — identitas agent
    # yang menyertakan versi KONFIGURASI (soul.toml efektif) saat approval ini
    # dibuat, bukan cuma nama role. None = caller belum menghitungnya.
    agent_identity: str | None = None
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
        # Tamper-evident audit trail (§ Prioritas 9.1). Checkpoint manusia adalah
        # bukti audit paling penting di produk ini — "apakah manusia benar-benar
        # menyetujui aksi destruktif ini, atau dilewati?" harus bisa dibuktikan,
        # bukan cuma dipercaya dari baris DB yang bisa diubah belakangan.
        self.chain = AuditChain(db)

    async def request(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        approval_id: str | None = None,
        owner_user_id: str | None = None,
        agent_identity: str | None = None,
    ) -> bool:
        """`approval_id` opsional — caller (AgentLoop) bisa pre-generate & emit ke UI
        SEBELUM memanggil ini, agar user tahu ID-nya sementara request() masih menunggu
        (§ chat approval UI). Default None → generate seperti sebelumnya (tak ada
        perubahan perilaku untuk caller lama).

        `owner_user_id` (audit produksi 2026-07-29): identitas user yang memicu
        approval ini, dipakai web/main.py untuk menggerbangi GET /approvals dan
        POST /approve agar user lain tak bisa lihat/putuskan approval ini.
        None (default) bila auth nonaktif atau caller tak menyertakan.

        `agent_identity` (§ Prioritas 9.2, Non-Human Identity): identitas agent
        (role + hash konfigurasi) yang memicu approval ini — melengkapi
        `owner_user_id` (siapa MANUSIA-nya) dengan "agent versi mana"."""
        approval_id = approval_id or uuid.uuid4().hex
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            owner_user_id=owner_user_id,
            agent_identity=agent_identity,
        )
        self._pending[approval_id] = pending

        # Catat permintaan dengan status pending — auditor & Web UI butuh ini.
        # approval_id di kolom SENDIRI (bukan encode di 'decision') agar tetap
        # query-able setelah keputusan final ditulis (§ Human Approval Pipeline,
        # TODO.md Prioritas 2) — GET /approval/{approval_id} bisa melacak satu
        # approval lintas status pending→approved/rejected/timeout.
        cursor = await self.db.execute(
            """INSERT INTO approval_log
               (session_id, tool_name, tool_input, decision, approval_id, owner_user_id, agent_identity)
               VALUES (?,?,?,?,?,?,?)""",
            (
                session_id,
                tool_name,
                json.dumps(tool_input),
                "pending",
                approval_id,
                owner_user_id,
                agent_identity,
            ),
        )
        await self.chain.append(
            ENTRY_APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "agent_identity": agent_identity,
                "owner_user_id": owner_user_id,
            },
            ref_table="approval_log",
            ref_id=cursor.lastrowid,
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
        cursor = await self.db.execute(
            "UPDATE approval_log SET decision=? WHERE approval_id=? AND decision='pending'",
            (decision, approval_id),
        )
        # rowcount==0: baris sudah diputuskan lebih dulu (mis. dua caller balapan
        # menyelesaikan approval yatim yang sama — § durable execution, Prioritas
        # 8.1) atau approval_id tak ada. Jangan tulis entry chain untuk keputusan
        # yang TAK BENAR-BENAR terjadi — sama pola `set_human_feedback`.
        if cursor.rowcount <= 0:
            return
        # Entry BARU, bukan mengubah entry 'requested' — pasangan
        # requested→decided inilah bukti bahwa checkpoint manusia benar-benar
        # dilalui (dan berapa lama), bukan diklaim setelah fakta.
        # ref_id None: ini UPDATE, tak ada lastrowid baru. Penghubung ke baris
        # approval_log adalah `approval_id` di payload (kolom query-able di sana),
        # bukan rowid — sama kunci yang dipakai GET /approval/{approval_id}.
        await self.chain.append(
            ENTRY_APPROVAL_DECIDED,
            {"approval_id": approval_id, "decision": decision},
            ref_table="approval_log",
            ref_id=None,
        )

    async def finalize_orphan(self, approval_id: str, decision: str) -> bool:
        """Selesaikan approval 'pending' YATIM (dibuat sebelum restart server
        terakhir — tak ada `asyncio.Future` in-memory lagi untuk di-resolve lewat
        `resolve()`) — § Durable execution, TODO.md Prioritas 8.1.

        Dipanggil `core/late_execute.py` dua kali: langsung untuk reject, atau
        SETELAH tool selesai dieksekusi mandiri untuk approve (`decision`
        biasanya `"approved:late"` agar audit trail membedakan dari approve
        langsung lewat klik saat sesi masih live). Return True bila baris
        memang masih pending & berhasil diselesaikan (rowcount>0), False bila
        sudah diputuskan lebih dulu (race dengan caller lain) — caller
        (`core/late_execute.py`) tetap sudah menjalankan tool dalam kasus race
        approve; itu risiko fail-soft yang diterima, bukan double-execute yang
        disengaja (approval_id unik per permintaan, race hanya bisa terjadi
        dari dua klik ganda pada tombol yang sama)."""
        if approval_id in self._pending:
            # Ada Future hidup — bukan orphan. Caller salah jalur (seharusnya
            # `resolve()`), tolak agar tak ada dua sumber kebenaran untuk satu
            # approval yang sama.
            return False
        before = await self.db.fetchone(
            "SELECT 1 FROM approval_log WHERE approval_id=? AND decision='pending'",
            (approval_id,),
        )
        if before is None:
            return False
        await self._record_decision(approval_id, decision)
        return True

    async def pending_list_with_orphans(
        self, session_id: str | None = None, owner_user_id: str | None = None
    ) -> list[dict]:
        """`pending_list()` (in-memory, live) DIGABUNG baris `approval_log` yang
        masih `decision='pending'` tapi TAK PUNYA Future in-memory lagi — yaitu
        approval yang dibuat sebelum restart server terakhir ("yatim", § Durable
        execution TODO.md Prioritas 8.1). Tanpa ini, `GET /approvals` cuma baca
        `self._pending` yang SELALU kosong tepat setelah proses baru mulai —
        approval yatim jadi tak terlihat & tak bisa di-resolve lagi selamanya,
        walau barisnya tetap ada di DB. Field `orphan: True` membedakan di UI
        (approve/reject approval ini lewat `POST /approve` tetap sama, endpoint
        yang menentukan jalur mana yang dipakai — lihat `web/main.py`).
        """
        live = self.pending_list(session_id, owner_user_id)
        live_ids = {p["approval_id"] for p in live}
        query = (
            "SELECT approval_id, session_id, tool_name, tool_input, owner_user_id "
            "FROM approval_log WHERE decision='pending'"
        )
        params: list[str] = []
        if session_id is not None:
            query += " AND session_id=?"
            params.append(session_id)
        rows = await self.db.fetchall(query, tuple(params))
        orphans = []
        for row in rows:
            approval_id = row["approval_id"]
            if not approval_id or approval_id in live_ids:
                continue
            row_owner = row["owner_user_id"]
            if owner_user_id is not None and row_owner is not None and row_owner != owner_user_id:
                continue
            orphans.append(
                {
                    "approval_id": approval_id,
                    "session_id": row["session_id"],
                    "tool_name": row["tool_name"],
                    "tool_input": json.loads(row["tool_input"]) if row["tool_input"] else {},
                    "orphan": True,
                }
            )
        return live + orphans

    async def auto_approve(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        agent_identity: str | None = None,
    ) -> bool:
        """Setuju otomatis untuk "Trust mode" per-sesi (§ user request otonomi).

        BEDA dari `queue_proposal` (autopilot — tanpa manusia, TIDAK dieksekusi,
        jadi proposal): trust mode berarti manusia SEDANG hadir di sesi chat aktif
        dan secara sadar memilih melewati klik Approve — tool tetap DIEKSEKUSI
        sungguhan. Tetap tercatat di approval_log (decision="auto:trust_mode",
        bukan "approved" biasa) agar audit trail membedakan keputusan manual vs
        toggle trust mode. Selalu return True — caller (AgentLoop) yang memutuskan
        tool mana yang boleh lewat sini (code_run TIDAK PERNAH, CLAUDE.md §1).

        `agent_identity` (§ Prioritas 9.2) — dicatat SEKALIGUS penting di jalur ini:
        approval yang MELEWATI klik manusia adalah tepat jalur yang paling perlu
        bisa dijawab "agent versi mana yang melakukannya".
        """
        cursor = await self.db.execute(
            """INSERT INTO approval_log (session_id, tool_name, tool_input, decision, agent_identity)
               VALUES (?,?,?,?,?)""",
            (session_id, tool_name, json.dumps(tool_input), "auto:trust_mode", agent_identity),
        )
        # DIRANTAI justru karena ini MELEWATI klik manusia — "aksi butuh-approval
        # mana yang dijalankan tanpa persetujuan eksplisit" adalah pertanyaan
        # pertama auditor, dan jawabannya harus tak bisa dihapus diam-diam.
        await self.chain.append(
            ENTRY_APPROVAL_AUTO,
            {"session_id": session_id, "tool_name": tool_name, "agent_identity": agent_identity},
            ref_table="approval_log",
            ref_id=cursor.lastrowid,
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
