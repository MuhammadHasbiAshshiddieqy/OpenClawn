"""Tests untuk QuestionGate (ask_user interaktif) + integrasi AgentLoop._execute_tool."""

import asyncio

import pytest

from infra.config import AppConfig
from security.question import QuestionGate


@pytest.fixture
def gate():
    # timeout pendek agar test timeout tidak lambat
    return QuestionGate(AppConfig(approval_timeout_sec=1))


async def test_ask_resolved_returns_answer(gate):
    """ask() menunggu Future; resolve() dari 'UI' memberi jawaban."""

    async def answer_soon():
        await asyncio.sleep(0.01)
        # satu pertanyaan pending → resolve_by_session menjawabnya
        assert gate.resolve_by_session("s1", "biru")

    asyncio.create_task(answer_soon())
    result = await gate.ask("s1", "Warna favorit?")
    assert result == "biru"


async def test_ask_timeout_returns_no_answer(gate):
    """Tidak ada jawaban dalam batas waktu → NO_ANSWER (fail-soft, bukan hang)."""
    result = await gate.ask("s2", "Pertanyaan tanpa jawaban?")
    assert result == QuestionGate.NO_ANSWER


async def test_resolve_by_id(gate):
    """resolve(question_id) bekerja; pending_list mengekspos id."""

    async def answer_soon():
        await asyncio.sleep(0.01)
        pending = gate.pending_list("s3")
        assert len(pending) == 1
        assert gate.resolve(pending[0]["question_id"], "jawaban-by-id")

    asyncio.create_task(answer_soon())
    result = await gate.ask("s3", "Q?")
    assert result == "jawaban-by-id"


def test_resolve_unknown_session_returns_false(gate):
    """resolve_by_session untuk sesi tanpa pertanyaan → False (tidak crash)."""
    assert gate.resolve_by_session("ghost", "x") is False


def test_resolve_unknown_id_returns_false(gate):
    """resolve dengan id tak dikenal → False."""
    assert gate.resolve("nonexistent", "x") is False


async def test_pending_list_filters_by_session(gate):
    """pending_list menyaring per session."""

    async def collect():
        await asyncio.sleep(0.01)
        assert len(gate.pending_list("sa")) == 1
        assert len(gate.pending_list("sb")) == 1
        assert len(gate.pending_list()) == 2  # semua
        gate.resolve_by_session("sa", "a")
        gate.resolve_by_session("sb", "b")

    asyncio.create_task(collect())
    a, b = await asyncio.gather(gate.ask("sa", "Qa?"), gate.ask("sb", "Qb?"))
    assert a == "a" and b == "b"


# ── Integrasi: ask_user lewat AgentLoop._execute_tool (bukan stub lama) ──────


async def test_agent_loop_ask_user_uses_gate():
    """_execute_tool('ask_user') menunggu QuestionGate, bukan kembalikan stub."""
    from core.agent_loop import AgentConfig, AgentLoop
    from infra.config import AppConfig
    from infra.database import DatabaseManager

    cfg = AppConfig(db_path=":memory:", approval_timeout_sec=1)
    db = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await db.conn()
        await conn.executescript(f.read())
        await conn.commit()

    gate = QuestionGate(cfg)
    # role 'pm' mengizinkan ask_user (cek soul); inject gate yang sama.
    agent = AgentLoop(
        AgentConfig(role="pm", session_id="qs1"), db=db, config=cfg, question_gate=gate
    )

    async def _user_answers():
        await asyncio.sleep(0.05)
        gate.resolve_by_session("qs1", "pengguna B2B")

    asyncio.create_task(_user_answers())
    result = await agent._execute_tool("ask_user", {"question": "Target user?"})
    assert result == {"answer": "pengguna B2B"}

    await db.close()


async def test_agent_loop_ask_user_empty_question():
    """ask_user tanpa question → error, tidak menggantung."""
    from core.agent_loop import AgentConfig, AgentLoop
    from infra.config import AppConfig
    from infra.database import DatabaseManager

    cfg = AppConfig(db_path=":memory:", approval_timeout_sec=1)
    db = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await db.conn()
        await conn.executescript(f.read())
        await conn.commit()

    agent = AgentLoop(AgentConfig(role="pm", session_id="qs2"), db=db, config=cfg)
    result = await agent._execute_tool("ask_user", {"question": "   "})
    assert "error" in result

    await db.close()
