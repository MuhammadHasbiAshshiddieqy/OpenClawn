"""Durable execution — orphan cleanup + late-execute (TODO.md § Prioritas 8.1).

`ApprovalGate._pending` cuma in-memory: begitu server restart, baris
`approval_log` yang masih `decision='pending'` tak punya `asyncio.Future` lagi
untuk di-`resolve()` — sebelumnya jadi tak terlihat & tak bisa diputuskan
SELAMANYA. Simulasi "restart" di test ini: buat `ApprovalGate` BARU (`_pending`
kosong) di atas DB yang sudah punya baris pending dari gate/insert sebelumnya —
persis kondisi proses baru yang membaca DB lama.
"""

import json

import pytest

from core.late_execute import execute_orphan_approval
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.workspace import SessionWorkspaceStore
from security.approval import ApprovalGate


@pytest.fixture
async def db(tmp_path):
    config = AppConfig(db_path=":memory:", workspace_root=str(tmp_path))
    manager = DatabaseManager(config)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
    await conn.commit()
    yield manager, config
    await manager.close()


async def _insert_pending(manager, approval_id, session_id, tool_name, tool_input, owner=None):
    await manager.execute(
        """INSERT INTO approval_log
           (session_id, tool_name, tool_input, decision, approval_id, owner_user_id)
           VALUES (?,?,?,?,?,?)""",
        (session_id, tool_name, json.dumps(tool_input), "pending", approval_id, owner),
    )


async def _insert_session(manager, session_id, role):
    await manager.execute(
        "INSERT INTO chat_sessions (session_id, role) VALUES (?, ?)", (session_id, role)
    )


@pytest.mark.asyncio
async def test_orphan_visible_in_pending_list_with_orphans(db):
    manager, config = db
    await _insert_pending(manager, "orph-1", "s1", "file_write", {"path": "a.txt", "content": "x"})
    gate = ApprovalGate(manager, config)  # "restart" — _pending kosong

    result = gate.pending_list()
    assert result == []  # jalur lama tak melihat apa-apa — inilah gap-nya

    with_orphans = await gate.pending_list_with_orphans()
    assert len(with_orphans) == 1
    assert with_orphans[0]["approval_id"] == "orph-1"
    assert with_orphans[0]["orphan"] is True


@pytest.mark.asyncio
async def test_live_pending_not_duplicated_as_orphan(db):
    import asyncio

    manager, config = db
    gate = ApprovalGate(manager, config)
    task = asyncio.create_task(gate.request("s1", "file_write", {"path": "a"}, "live-1"))
    await asyncio.sleep(0.02)

    result = await gate.pending_list_with_orphans()
    assert len(result) == 1
    assert result[0]["approval_id"] == "live-1"
    assert "orphan" not in result[0]

    gate.resolve("live-1", True)
    await task


@pytest.mark.asyncio
async def test_orphan_respects_owner_filter(db):
    manager, config = db
    await _insert_pending(manager, "orph-owned", "s1", "file_write", {}, owner="user-a")
    gate = ApprovalGate(manager, config)

    as_owner = await gate.pending_list_with_orphans(owner_user_id="user-a")
    as_other = await gate.pending_list_with_orphans(owner_user_id="user-b")
    assert len(as_owner) == 1
    assert as_other == []


@pytest.mark.asyncio
async def test_finalize_orphan_rejects_when_future_still_live(db):
    import asyncio

    manager, config = db
    gate = ApprovalGate(manager, config)
    task = asyncio.create_task(gate.request("s1", "file_write", {}, "live-2"))
    await asyncio.sleep(0.02)

    ok = await gate.finalize_orphan("live-2", "rejected")
    assert ok is False  # bukan orphan — caller salah jalur

    gate.resolve("live-2", True)
    await task


@pytest.mark.asyncio
async def test_finalize_orphan_false_when_already_decided(db):
    manager, config = db
    await _insert_pending(manager, "orph-2", "s1", "file_write", {})
    gate = ApprovalGate(manager, config)

    first = await gate.finalize_orphan("orph-2", "rejected")
    second = await gate.finalize_orphan("orph-2", "rejected")
    assert first is True
    assert second is False  # sudah diputuskan — tak boleh menulis chain entry lagi


@pytest.mark.asyncio
async def test_execute_orphan_approval_happy_path_runs_tool(db, tmp_path):
    manager, config = db
    await _insert_session(manager, "s-dev", "dev")
    await _insert_pending(
        manager, "orph-exec", "s-dev", "file_write", {"path": "out.txt", "content": "hello"}
    )
    # § working directory adaptif: pulihkan folder kerja sesi dari DB, sama
    # mekanisme yang dipakai turn live — tanpa ini tool jatuh ke
    # CONFIG.workspace_root GLOBAL (singleton proses, bukan `config` fixture
    # ini), yang tidak deterministik untuk test.
    await SessionWorkspaceStore(manager).set("s-dev", str(tmp_path))
    gate = ApprovalGate(manager, config)

    outcome = await execute_orphan_approval(manager, config, gate, "orph-exec")

    assert outcome["ok"] is True
    assert outcome["executed"] is True
    assert outcome["result"]["ok"] is True
    written = tmp_path / "out.txt"
    assert written.read_text() == "hello"

    row = await manager.fetchone(
        "SELECT decision FROM approval_log WHERE approval_id=?", ("orph-exec",)
    )
    assert row["decision"] == "approved:late"


@pytest.mark.asyncio
async def test_execute_orphan_approval_unknown_session_fails_closed(db):
    manager, config = db
    await _insert_pending(manager, "orph-no-session", "ghost-session", "file_write", {})
    gate = ApprovalGate(manager, config)

    outcome = await execute_orphan_approval(manager, config, gate, "orph-no-session")

    assert outcome["ok"] is False
    row = await manager.fetchone(
        "SELECT decision FROM approval_log WHERE approval_id=?", ("orph-no-session",)
    )
    assert row["decision"] == "rejected"


@pytest.mark.asyncio
async def test_execute_orphan_approval_tool_not_allowed_for_role_fails_closed(db):
    manager, config = db
    # pm/soul.toml tidak mengizinkan code_run — lihat allow-list role itu.
    await _insert_session(manager, "s-pm", "pm")
    await _insert_pending(manager, "orph-forbidden", "s-pm", "code_run", {"code": "1+1"})
    gate = ApprovalGate(manager, config)

    outcome = await execute_orphan_approval(manager, config, gate, "orph-forbidden")

    assert outcome["ok"] is False
    assert "tidak diizinkan" in outcome["error"]


@pytest.mark.asyncio
async def test_execute_orphan_approval_reevaluates_policy_deny(db, tmp_path, monkeypatch):
    """Policy bisa berubah SELAMA approval tersangkut (§ core/late_execute.py
    docstring) — deny dievaluasi ULANG saat late-execute, bukan dipercaya dari
    keputusan lama, jadi tool sungguhan TIDAK dijalankan (file tak tertulis)."""
    manager, config = db
    await _insert_session(manager, "s-dev2", "dev")
    await _insert_pending(
        manager, "orph-policy", "s-dev2", "file_write", {"path": "x.txt", "content": "y"}
    )
    await SessionWorkspaceStore(manager).set("s-dev2", str(tmp_path))
    gate = ApprovalGate(manager, config)

    from dataclasses import dataclass

    @dataclass
    class _DenyDecision:
        action: str = "deny"
        reason: str = "test policy deny"

    class _DenyEngine:
        def __init__(self, *_a, **_kw):
            pass

        def evaluate(self, *_a, **_kw):
            return _DenyDecision()

    monkeypatch.setattr("core.late_execute.PolicyEngine", _DenyEngine)

    outcome = await execute_orphan_approval(manager, config, gate, "orph-policy")

    assert outcome["ok"] is False
    assert "policy" in outcome["error"]
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_execute_orphan_approval_does_not_double_execute_live_future(db):
    import asyncio

    manager, config = db
    await _insert_session(manager, "s-live", "dev")
    gate = ApprovalGate(manager, config)
    task = asyncio.create_task(gate.request("s-live", "file_write", {"path": "z"}, "live-3"))
    await asyncio.sleep(0.02)

    outcome = await execute_orphan_approval(manager, config, gate, "live-3")
    assert outcome["ok"] is False  # masih live — bukan jalur ini

    gate.resolve("live-3", True)
    await task
