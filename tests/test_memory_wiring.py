"""Tests integrasi: agent_loop benar-benar MENULIS ke memori jangka panjang.

Regression guard untuk celah yang ditemukan di Sprint 4 hardening: MemoryManager
punya update_checkpoint (L1) & archive_session (L4) tapi _post_turn tak pernah
memanggilnya — agent membaca memori tiap turn namun tak pernah menulis.
"""

import pytest
from infra.config import AppConfig
from infra.database import DatabaseManager
from core.agent_loop import AgentLoop, AgentConfig, Turn


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


def _agent(db, cfg):
    return AgentLoop(AgentConfig(role="pm", session_id="s-mem"), db=db, config=cfg)


@pytest.mark.asyncio
async def test_post_turn_writes_l1_checkpoint(db):
    """Tiap turn dengan konten → L1 checkpoint ter-update (bukan kosong)."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=99)  # cegah L4 di test ini
    agent = _agent(db, cfg)
    turn = Turn(role="assistant", content="ringkasan hasil", model_used="gemma4:e4b")

    await agent._post_turn("pesan", turn, [], [turn])

    row = await db.fetchone("SELECT key, value FROM memory_l1 WHERE role='pm'")
    assert row is not None, "L1 checkpoint harus tertulis setelah turn"
    assert row["key"] == "last_summary"
    assert row["value"] == "ringkasan hasil"


@pytest.mark.asyncio
async def test_post_turn_empty_content_no_checkpoint(db):
    """Turn tanpa konten (mis. hanya tool call) → tidak menulis L1 kosong."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=99)
    agent = _agent(db, cfg)
    turn = Turn(role="assistant", content="", model_used="gemma4:e4b")

    await agent._post_turn("pesan", turn, [], [turn])

    rows = await db.fetchall("SELECT value FROM memory_l1 WHERE role='pm'")
    assert rows == []


@pytest.mark.asyncio
async def test_post_turn_archives_l4_after_threshold(db):
    """History melewati ambang → sesi diarsipkan ke L4 untuk cross-session search."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=4)
    agent = _agent(db, cfg)
    turn = Turn(role="assistant", content="kesimpulan sesi", model_used="gemma4:e4b")
    history = [
        Turn(role="user", content="q1"),
        Turn(role="assistant", content="a1"),
        Turn(role="user", content="q2"),
        turn,
    ]

    await agent._post_turn("q2", turn, [], history)

    rows = await db.fetchall("SELECT summary, full_content FROM memory_l4 WHERE role='pm'")
    assert len(rows) == 1
    assert "kesimpulan sesi" in rows[0]["summary"]
    # full_content harus berisi transkrip yang bisa di-search
    assert "q1" in rows[0]["full_content"]


@pytest.mark.asyncio
async def test_post_turn_no_archive_below_threshold(db):
    """History pendek (< ambang) → belum diarsipkan ke L4."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=6)
    agent = _agent(db, cfg)
    turn = Turn(role="assistant", content="x", model_used="gemma4:e4b")

    await agent._post_turn("p", turn, [], [turn])

    rows = await db.fetchall("SELECT summary FROM memory_l4 WHERE role='pm'")
    assert rows == []


@pytest.mark.asyncio
async def test_repeated_archive_no_duplicates(db):
    """Arsip dipanggil berulang untuk sesi sama → idempoten, tidak menumpuk duplikat."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=2)
    agent = _agent(db, cfg)
    history = [Turn(role="user", content="q"), Turn(role="assistant", content="a1")]

    turn1 = Turn(role="assistant", content="versi 1", model_used="gemma4:e4b")
    await agent._post_turn("q", turn1, [], history + [turn1])

    turn2 = Turn(role="assistant", content="versi 2", model_used="gemma4:e4b")
    await agent._post_turn("q", turn2, [], history + [turn1, turn2])

    rows = await db.fetchall("SELECT summary FROM memory_l4 WHERE role='pm'")
    assert len(rows) == 1, "harus tetap satu arsip per sesi (idempoten)"
    assert "versi 2" in rows[0]["summary"], "arsip harus versi terbaru"


@pytest.mark.asyncio
async def test_written_memory_is_readable_next_turn(db):
    """End-to-end: memori yang ditulis turn ini terbaca di load_context turn berikut."""
    cfg = AppConfig(db_path=":memory:", archive_after_turns=99)
    agent = _agent(db, cfg)
    turn = Turn(role="assistant", content="state penting", model_used="gemma4:e4b")

    await agent._post_turn("pesan", turn, [], [turn])

    ctx = await agent.memory.load_context(query="apa saja", skills=[])
    assert ctx["l1"].get("last_summary") == "state penting"
