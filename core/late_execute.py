"""Eksekusi mandiri satu approval 'pending' YATIM (§ Durable execution, TODO.md
Prioritas 8.1) — dipanggil `web/main.py::POST /approve` saat approval_id yang
di-approve/reject sudah tak punya `asyncio.Future` in-memory (`ApprovalGate._pending`)
lagi, yaitu dibuat SEBELUM restart server terakhir.

Keputusan desain (dipilih owner, bukan resume percakapan penuh): `AgentLoop.run()`
untuk turn ASLI yang menunggu approval itu sudah selesai/hilang begitu proses lama
mati — tak ada tool loop untuk di-resume. Yang MASIH bisa & berguna: approval yang
tersangkut dibuat terlihat lagi (`ApprovalGate.pending_list_with_orphans`) dan
tool-nya BENAR-BENAR dijalankan mandiri saat user approve — bukan cuma dicatat lalu
didiamkan. Hasil dikembalikan langsung ke respons `POST /approve`, BUKAN dikirim ke
sesi chat asli (SSE turn itu sudah tak ada) — user perlu tanya ulang di chat bila
ingin agent melanjutkan dari hasil ini.

Fail-closed di setiap langkah (§1): role/soul/policy yang tak bisa diverifikasi
ulang → tolak eksekusi, jangan asumsikan aman hanya karena baris ini pernah lolos
requires_approval check saat pertama diminta (soul.toml/policy bisa berubah selama
approval tersangkut, kadang berbulan-bulan)."""

import asyncio
import json
import time
import tomllib

from core.agent_loop import _soul_allows_tool, _validate_tool_input
from core.tool_audit import ToolAudit
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.logging import log
from infra.workspace import CURRENT_WORKSPACE_ROOT, SessionWorkspaceStore
from security.approval import ApprovalGate
from security.policy_engine import PolicyEngine
from security.vault import Vault
from tools import TOOL_REGISTRY


async def execute_orphan_approval(
    db: DatabaseManager,
    config: AppConfig,
    approval_gate: ApprovalGate,
    approval_id: str,
) -> dict:
    """Jalankan tool dari satu approval yatim yang baru user approve.

    Return `{"ok": bool, "executed": bool, "result"?: dict, "error"?: str}`.
    `ok=False` berarti eksekusi TIDAK terjadi (approval sudah diputuskan lebih
    dulu, atau ditolak fail-closed) — beda dari `result={"error": ...}` yang
    berarti tool DIEKSEKUSI tapi gagal (mis. timeout tool itu sendiri).
    """
    row = await db.fetchone(
        """SELECT session_id, tool_name, tool_input
           FROM approval_log WHERE approval_id=? AND decision='pending'""",
        (approval_id,),
    )
    if row is None:
        return {"ok": False, "error": "approval tidak ditemukan atau sudah diputuskan"}
    if approval_gate.find_pending(approval_id) is not None:
        return {"ok": False, "error": "approval ini masih live di sesi aktif — pakai alur biasa"}

    session_id = row["session_id"]
    tool_name = row["tool_name"]
    tool_input = json.loads(row["tool_input"]) if row["tool_input"] else {}

    session_row = await db.fetchone(
        "SELECT role FROM chat_sessions WHERE session_id=?", (session_id,)
    )
    if session_row is None:
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {
            "ok": False,
            "error": "sesi sumber approval ini tidak ditemukan — ditolak (fail-closed)",
        }
    role = session_row["role"]

    try:
        with open(f"roles/{role}/soul.toml", "rb") as f:
            soul = tomllib.load(f)
    except OSError:
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {
            "ok": False,
            "error": f"soul.toml role '{role}' tidak ditemukan — ditolak (fail-closed)",
        }

    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {"ok": False, "error": f"tool '{tool_name}' tidak dikenal"}
    if not _soul_allows_tool(soul, tool_name):
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {"ok": False, "error": f"tool '{tool_name}' tidak diizinkan untuk role '{role}'"}

    schema_err = _validate_tool_input(tool, tool_input)
    if schema_err:
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {"ok": False, "error": schema_err}

    # Policy bisa saja berubah SELAMA approval ini tersangkut (kadang berbulan-
    # bulan) — evaluasi ULANG deny di sini, jangan percaya keputusan lama.
    policy_decision = PolicyEngine(soul.get("policy", {})).evaluate(tool_name, tool_input)
    if policy_decision.action == "deny":
        await approval_gate.finalize_orphan(approval_id, "rejected")
        return {"ok": False, "error": f"ditolak oleh policy: {policy_decision.reason}"}

    # Folder kerja sesi (§ working directory adaptif) — dipulihkan dari DB
    # (session_workspace), sama mekanisme AgentLoop.run() memulai turn baru.
    # None bila sesi tak pernah pindah folder → tool jatuh ke CONFIG.workspace_root
    # (perilaku default, lihat infra/workspace.py::effective_workspace_root).
    workdir = await SessionWorkspaceStore(db).get(session_id)
    token = CURRENT_WORKSPACE_ROOT.set(workdir)
    outcome = "ok"
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            tool.execute(tool_input, vault=Vault(), db=db),
            timeout=config.tool_timeout_sec,
        )
    except asyncio.TimeoutError:
        outcome = "timeout"
        result = {"error": f"Tool '{tool_name}' melebihi batas waktu {config.tool_timeout_sec}s"}
    except Exception as exc:  # noqa: BLE001 — tool pihak ketiga, kegagalan harus anggun
        outcome = "error"
        log.error("orphan_tool_failed", tool=tool_name, error=str(exc), session=session_id)
        result = {"error": f"Tool '{tool_name}' gagal: {exc}"}
    finally:
        CURRENT_WORKSPACE_ROOT.reset(token)
        latency_ms = int((time.monotonic() - started) * 1000)
        await ToolAudit(db).record(session_id, role, tool_name, outcome, latency_ms)

    # "approved:late" (bukan "approved" biasa) agar audit trail membedakan
    # approve yang melalui sesi live vs. yang dieksekusi mandiri lintas restart.
    finalized = await approval_gate.finalize_orphan(approval_id, "approved:late")
    if not finalized:
        # Race: caller lain menyelesaikan approval_id ini di antara SELECT di
        # atas dan sini. Tool SUDAH terlanjur dieksekusi (fail-soft yang
        # diterima — lihat docstring `ApprovalGate.finalize_orphan`); tetap
        # laporkan hasil apa adanya, jangan sembunyikan bahwa itu terjadi.
        log.warning("orphan_approval_race", approval_id=approval_id, tool=tool_name)
    log.info(
        "orphan_approval_executed",
        approval_id=approval_id,
        tool=tool_name,
        session=session_id,
        outcome=outcome,
    )
    return {"ok": True, "executed": True, "result": result}
