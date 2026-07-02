"""Test riwayat percakapan per-sesi (§ user report: agent seolah tak baca chat sebelumnya).

AgentLoop dibuat baru tiap request web → self.history kosong. Tanpa persistensi,
turn N+1 tak melihat turn N walau di sesi yang sama. Tabel session_turns + load_turns/
append_turn memperbaikinya: giliran disimpan lalu dimuat kembali di AgentLoop berikutnya.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from core.agent_loop import AgentLoop, AgentConfig
from core.llm_client import LLMChunk
from memory.layers import MemoryManager


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


# ── MemoryManager: load_turns / append_turn ──────────────────────────────────


@pytest.mark.asyncio
async def test_append_and_load_turns_roundtrip(db):
    mm = MemoryManager("dev", "sess-A", db)
    await mm.append_turn("user", "Buat file hello.go")
    await mm.append_turn("assistant", "Selesai, file ada di /ws/hello.go")
    turns = await mm.load_turns()
    assert turns == [
        {"role": "user", "content": "Buat file hello.go"},
        {"role": "assistant", "content": "Selesai, file ada di /ws/hello.go"},
    ]


@pytest.mark.asyncio
async def test_load_turns_isolated_per_session(db):
    await MemoryManager("dev", "sess-A", db).append_turn("user", "halo A")
    await MemoryManager("dev", "sess-B", db).append_turn("user", "halo B")
    a = await MemoryManager("dev", "sess-A", db).load_turns()
    b = await MemoryManager("dev", "sess-B", db).load_turns()
    assert [t["content"] for t in a] == ["halo A"]
    assert [t["content"] for t in b] == ["halo B"]


@pytest.mark.asyncio
async def test_load_turns_caps_at_limit_keeping_newest(db):
    mm = MemoryManager("dev", "sess-C", db)
    for i in range(30):
        await mm.append_turn("user", f"msg {i}")
    turns = await mm.load_turns(limit=5)
    assert len(turns) == 5
    assert [t["content"] for t in turns] == [
        f"msg {i}" for i in range(25, 30)
    ]  # terbaru, urut lama→baru


@pytest.mark.asyncio
async def test_append_turn_skips_empty(db):
    mm = MemoryManager("dev", "sess-D", db)
    await mm.append_turn("assistant", "")
    assert await mm.load_turns() == []


# ── AgentLoop: history dimuat ulang antar request ────────────────────────────


def _tool_free_stream(reply: str):
    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        fake_stream.last_messages = [dict(m) for m in messages]
        yield LLMChunk(type="text", text=reply)

    fake_stream.last_messages = []
    return fake_stream


@pytest.mark.asyncio
async def test_second_agentloop_sees_prior_turn(db):
    """Turn 1 di satu AgentLoop → turn 2 di AgentLoop BARU (session sama) melihatnya."""
    sid = "sess-live"

    # Turn 1
    a1 = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    a1.llm.stream_with_fallback = _tool_free_stream("Sudah kubuat di ./hello.go")
    _ = [ev async for ev in a1.run("Buat file hello world golang")]

    # Turn 2 — AgentLoop BARU (mensimulasikan request web berikutnya), session sama.
    a2 = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    stream2 = _tool_free_stream("File ada di ./hello.go")
    a2.llm.stream_with_fallback = stream2
    _ = [ev async for ev in a2.run("Mana file-nya?")]

    # messages yang dikirim ke LLM pada turn 2 HARUS memuat turn 1 (user+assistant).
    contents = [m.get("content", "") for m in stream2.last_messages]
    joined = "\n".join(contents)
    assert "Buat file hello world golang" in joined, "pesan user turn-1 hilang dari konteks"
    assert "Sudah kubuat di ./hello.go" in joined, "jawaban assistant turn-1 hilang dari konteks"
    # Dan pesan baru turn-2 tetap ada sebagai giliran user terakhir.
    assert stream2.last_messages[-1] == {"role": "user", "content": "Mana file-nya?"}


@pytest.mark.asyncio
async def test_persist_history_false_does_not_load_or_store(db):
    """Multi-agent (persist_history=False): tak memuat & tak menyimpan session_turns."""
    sid = "sess-multi"
    # Seed satu turn lewat MemoryManager langsung.
    await MemoryManager("dev", sid, db).append_turn("user", "turn lama")

    a = AgentLoop(AgentConfig(role="dev", session_id=sid, persist_history=False), db=db)
    stream = _tool_free_stream("jawaban")
    a.llm.stream_with_fallback = stream
    _ = [ev async for ev in a.run("pesan baru")]

    # Tak memuat: history di messages tak memuat 'turn lama'.
    joined = "\n".join(m.get("content", "") for m in stream.last_messages)
    assert "turn lama" not in joined
    # Tak menyimpan: session_turns tetap hanya 1 baris (seed), bukan bertambah.
    rows = await db.fetchall("SELECT content FROM session_turns WHERE session_id=?", (sid,))
    assert [r["content"] for r in rows] == ["turn lama"]
