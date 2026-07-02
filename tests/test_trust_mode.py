"""Test trust mode per-sesi (§ user request otonomi: kurangi approval yang tak perlu).

Tiga bagian:
1. shell_run tidak lagi butuh approval sama sekali (sandbox = pertahanan, bukan approval).
2. AgentConfig.trust_mode melewati approval manual untuk tool yang membutuhkannya,
   TAPI tool tetap benar-benar dieksekusi (beda dari autopilot yang hanya propose).
3. code_run (_TRUST_MODE_EXEMPT) TIDAK PERNAH bisa dilewati trust_mode — CLAUDE.md §1.
"""

import pytest
from unittest.mock import AsyncMock

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.workspace import CURRENT_WORKSPACE_ROOT
from core.agent_loop import AgentLoop, AgentConfig, _TRUST_MODE_EXEMPT
from tools.shell import ShellRunTool
from tools.code import CodeRunTool


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


# ── shell_run: approval dicabut ──────────────────────────────────────────────


def test_shell_run_no_longer_requires_approval():
    assert ShellRunTool.requires_approval is False


def test_code_run_still_requires_approval():
    """Kontrol negatif: code_run TIDAK ikut dilonggarkan (CLAUDE.md §1)."""
    assert CodeRunTool.requires_approval is True


def test_code_run_is_trust_mode_exempt():
    assert "code_run" in _TRUST_MODE_EXEMPT


# ── AgentLoop._execute_tool: trust_mode bypass ───────────────────────────────


@pytest.mark.asyncio
async def test_trust_mode_bypasses_approval_and_executes_for_real(db, tmp_path):
    """Trust mode aktif → file_write TETAP DIEKSEKUSI (bukan cuma diloloskan), lewat
    auto_approve (bukan request() yang blocking menunggu klik manusia).

    _execute_tool dipanggil langsung (bukan lewat run()), jadi CURRENT_WORKSPACE_ROOT
    di-set manual di sini agar file_write menulis ke tmp_path — BUKAN CONFIG.workspace_root
    asli (yang di-set run() via workspace_override, tak berjalan di jalur pendek ini).
    """
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="s-trust", workspace_override=str(tmp_path)),
        db=db,
    )
    agent.cfg.trust_mode = True
    agent.approval.request = AsyncMock(side_effect=AssertionError("request() tak boleh dipanggil"))

    token = CURRENT_WORKSPACE_ROOT.set(str(tmp_path))
    try:
        result = await agent._execute_tool(
            "file_write",
            {"path": "hello.go", "content": "package main"},
            bypass_approval=True,
        )
    finally:
        CURRENT_WORKSPACE_ROOT.reset(token)

    assert result.get("ok") is True
    assert (tmp_path / "hello.go").read_text() == "package main"
    # Tercatat di audit trail sebagai keputusan trust mode, bukan approval manual.
    rows = await db.fetchall(
        "SELECT decision FROM approval_log WHERE session_id=? AND tool_name='file_write'",
        ("s-trust",),
    )
    assert rows and rows[-1]["decision"] == "auto:trust_mode"


@pytest.mark.asyncio
async def test_trust_mode_never_bypasses_code_run(db):
    """code_run TETAP lewat approval.request() normal walau trust_mode=True &
    bypass_approval=True diteruskan caller — _execute_tool sendiri yang menjaga."""
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-trust-code"), db=db)
    agent.cfg.trust_mode = True
    agent.approval.request = AsyncMock(return_value=False)  # simulasikan ditolak/timeout
    agent.approval.auto_approve = AsyncMock(
        side_effect=AssertionError("auto_approve tak boleh dipanggil untuk code_run")
    )

    result = await agent._execute_tool("code_run", {"code": "print(1)"}, bypass_approval=True)

    agent.approval.request.assert_awaited_once()
    assert "ditolak" in result.get("error", "")


@pytest.mark.asyncio
async def test_bypass_approval_false_uses_normal_request(db, tmp_path):
    """bypass_approval=False (trust mode mati) → jalur approval.request() biasa."""
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="s-normal", workspace_override=str(tmp_path)),
        db=db,
    )
    agent.approval.request = AsyncMock(return_value=True)
    agent.approval.auto_approve = AsyncMock(
        side_effect=AssertionError("auto_approve tak boleh dipanggil saat trust mode mati")
    )

    token = CURRENT_WORKSPACE_ROOT.set(str(tmp_path))
    try:
        result = await agent._execute_tool(
            "file_write",
            {"path": "x.txt", "content": "hi"},
            bypass_approval=False,
        )
    finally:
        CURRENT_WORKSPACE_ROOT.reset(token)

    agent.approval.request.assert_awaited_once()
    assert result.get("ok") is True
    assert (tmp_path / "x.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_autopilot_wins_over_trust_mode(db):
    """Autopilot (tanpa manusia) tetap PROPOSAL — trust_mode tak relevan di sana."""
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="s-auto", autopilot=True, trust_mode=True), db=db
    )
    agent.approval.auto_approve = AsyncMock(
        side_effect=AssertionError("auto_approve tak boleh dipanggil saat autopilot")
    )

    result = await agent._execute_tool(
        "file_write", {"path": "x.txt", "content": "hi"}, bypass_approval=True
    )

    assert result.get("proposed") is True


# ── ApprovalGate.auto_approve ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_approve_records_trust_decision_and_returns_true(db):
    from security.approval import ApprovalGate

    gate = ApprovalGate(db, AppConfig(db_path=":memory:"))
    approved = await gate.auto_approve("s-x", "shell_run", {"command": "grep -rn TODO ."})
    assert approved is True
    row = await db.fetchone(
        "SELECT decision, tool_name FROM approval_log WHERE session_id=?", ("s-x",)
    )
    assert row["decision"] == "auto:trust_mode"
    assert row["tool_name"] == "shell_run"
