"""Test sidebar riwayat chat (§ user report: chat selalu ke-reset, tak ada cara
buka chat baru/lanjutkan/hapus riwayat).

Empat bagian:
1. truncate_for_title_prompt: pemotongan pesan panjang (§ user request — kirim
   head+tail kata, bukan pesan penuh, ke LLM pembuat judul).
2. ChatSessionStore: CRUD metadata sesi untuk sidebar.
3. Integrasi AgentLoop: judul di-generate sekali di turn pertama.
4. Endpoint web: GET /chat-sessions (+bucket waktu), GET .../turns, DELETE.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.chat_sessions import ChatSessionStore, truncate_for_title_prompt
from core.agent_loop import AgentLoop, AgentConfig, Turn
from core.llm_client import LLMChunk


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


# ── truncate_for_title_prompt ─────────────────────────────────────────────────


def test_truncate_short_message_unchanged():
    msg = "Buat file hello world golang"
    assert truncate_for_title_prompt(msg) == msg


def test_truncate_long_message_keeps_head_and_tail():
    words = [f"kata{i}" for i in range(50)]
    msg = " ".join(words)
    out = truncate_for_title_prompt(msg)
    assert out.startswith(" ".join(words[:20]))
    assert out.endswith(" ".join(words[-10:]))
    assert "..." in out
    # Jauh lebih pendek dari pesan asli — tak mengirim semuanya ke LLM.
    assert len(out.split()) < len(words)


def test_truncate_exact_boundary_unchanged():
    """Persis head+tail kata (30) → tidak dipotong (kondisi batas <=)."""
    words = [f"w{i}" for i in range(30)]
    msg = " ".join(words)
    assert truncate_for_title_prompt(msg) == msg


# ── ChatSessionStore ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_created_idempotent(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    await store.ensure_created("s1", "pm")  # panggilan kedua tak boleh error/duplikat
    sessions = await store.list_active()
    assert len(sessions) == 1
    assert sessions[0]["role"] == "pm"


@pytest.mark.asyncio
async def test_ensure_created_does_not_overwrite_existing_title(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    await store.set_title("s1", "Judul Lama")
    await store.ensure_created("s1", "pm")  # INSERT OR IGNORE — tak menimpa
    sessions = await store.list_active()
    assert sessions[0]["title"] == "Judul Lama"


@pytest.mark.asyncio
async def test_set_title_strips_quotes_and_truncates(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    long_title = '"' + ("x" * 100) + '"'
    await store.set_title("s1", long_title)
    sessions = await store.list_active()
    assert not sessions[0]["title"].startswith('"')
    assert len(sessions[0]["title"]) <= 60


@pytest.mark.asyncio
async def test_has_title_false_until_set(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    assert await store.has_title("s1") is False
    await store.set_title("s1", "Judul")
    assert await store.has_title("s1") is True


@pytest.mark.asyncio
async def test_touch_updates_timestamp(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    before = (await store.list_active())[0]["updated_at"]
    await db.execute(
        "UPDATE chat_sessions SET updated_at=datetime('now', '-1 hour') WHERE session_id='s1'"
    )
    await store.touch("s1")
    after = (await store.list_active())[0]["updated_at"]
    assert after != before or after >= before


@pytest.mark.asyncio
async def test_list_active_excludes_deleted(db):
    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    await store.ensure_created("s2", "dev")
    await store.soft_delete("s1")
    sessions = await store.list_active()
    assert [s["session_id"] for s in sessions] == ["s2"]


@pytest.mark.asyncio
async def test_soft_delete_hard_deletes_turns_and_workspace(db):
    from memory.layers import MemoryManager
    from infra.workspace import SessionWorkspaceStore

    store = ChatSessionStore(db)
    await store.ensure_created("s1", "pm")
    await MemoryManager("pm", "s1", db).append_turn("user", "halo")
    await SessionWorkspaceStore(db).set("s1", "/tmp")

    await store.soft_delete("s1")

    turns = await db.fetchall("SELECT * FROM session_turns WHERE session_id='s1'")
    assert turns == []
    ws = await db.fetchone("SELECT * FROM session_workspace WHERE session_id='s1'")
    assert ws is None
    # Metadata TETAP ada (soft-delete), hanya deleted_at terisi.
    row = await db.fetchone("SELECT deleted_at FROM chat_sessions WHERE session_id='s1'")
    assert row["deleted_at"] is not None


@pytest.mark.asyncio
async def test_list_active_respects_limit(db):
    store = ChatSessionStore(db)
    for i in range(5):
        await store.ensure_created(f"s{i}", "pm")
    sessions = await store.list_active(limit=3)
    assert len(sessions) == 3


# ── Integrasi AgentLoop: generate judul turn pertama ─────────────────────────


def _title_only_stream(title_text: str):
    """Fake stream_with_fallback yang HANYA dipanggil oleh _generate_session_title
    (bukan lewat agent.run() penuh — _post_turn adalah background task terpisah,
    lihat core/agent_loop.py:_run() langkah 8, jadi diuji langsung & di-await
    seperti tests/test_memory_wiring.py, bukan menunggu task latar belakang)."""

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        yield LLMChunk(type="text", text=title_text)

    return fake_stream


@pytest.mark.asyncio
async def test_title_generated_on_first_turn(db):
    sid = "sess-title"
    await ChatSessionStore(db).ensure_created(sid, "pm")
    agent = AgentLoop(AgentConfig(role="pm", session_id=sid), db=db)
    agent.llm.stream_with_fallback = _title_only_stream("Diskusi fitur baru")
    turn = Turn(role="assistant", content="Baik, saya bantu.", model_used="gemma4:e4b")

    await agent._post_turn("Saya ingin diskusi soal fitur baru untuk aplikasi", turn, [], [turn])

    sessions = await ChatSessionStore(db).list_active()
    assert sessions[0]["title"] == "Diskusi fitur baru"


@pytest.mark.asyncio
async def test_title_not_regenerated_on_second_turn(db):
    """Turn kedua TIDAK memanggil LLM lagi untuk judul (hemat token — hanya sekali)."""
    sid = "sess-title-2"
    await ChatSessionStore(db).ensure_created(sid, "pm")
    await ChatSessionStore(db).set_title(sid, "Judul Awal")

    agent = AgentLoop(AgentConfig(role="pm", session_id=sid), db=db)
    call_count = {"n": 0}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        call_count["n"] += 1
        yield LLMChunk(type="text", text="tak boleh dipanggil")

    agent.llm.stream_with_fallback = fake_stream
    turn = Turn(role="assistant", content="jawaban turn kedua", model_used="gemma4:e4b")
    await agent._post_turn("pesan kedua", turn, [], [turn])

    # has_title() sudah True → _generate_session_title TAK dipanggil sama sekali.
    assert call_count["n"] == 0
    sessions = await ChatSessionStore(db).list_active()
    assert sessions[0]["title"] == "Judul Awal"


@pytest.mark.asyncio
async def test_title_generation_failure_does_not_crash_turn(db):
    """LLM judul gagal (exception) → _post_turn tetap selesai normal (fail-safe §1.3)."""
    sid = "sess-title-fail"
    await ChatSessionStore(db).ensure_created(sid, "pm")
    agent = AgentLoop(AgentConfig(role="pm", session_id=sid), db=db)

    async def failing_stream(provider, model, messages, tools=None, max_tokens=4096):
        raise RuntimeError("llm down")
        yield  # pragma: no cover — buat ini async generator

    agent.llm.stream_with_fallback = failing_stream
    turn = Turn(role="assistant", content="jawaban", model_used="gemma4:e4b")

    await agent._post_turn("halo", turn, [], [turn])  # tidak boleh raise

    assert await ChatSessionStore(db).has_title(sid) is False  # judul gagal, tak tersimpan


@pytest.mark.asyncio
async def test_multi_agent_does_not_generate_title(db):
    """persist_history=False (multi-agent) → tak sentuh chat_sessions sama sekali."""
    sid = "sess-multi"
    agent = AgentLoop(AgentConfig(role="pm", session_id=sid, persist_history=False), db=db)
    call_count = {"n": 0}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        call_count["n"] += 1
        yield LLMChunk(type="text", text="tak boleh dipanggil")

    agent.llm.stream_with_fallback = fake_stream
    turn = Turn(role="assistant", content="jawaban", model_used="gemma4:e4b")
    await agent._post_turn("halo", turn, [], [turn])

    assert call_count["n"] == 0
    sessions = await ChatSessionStore(db).list_active()
    assert sessions == []


# ── Endpoint web: GET /chat-sessions, GET .../turns, DELETE ──────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace sementara (pola sama test_file_download.py)."""
    import importlib

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("OPENCLAWN_DB", str(db_file))
    monkeypatch.setenv("OPENCLAWN_WORKSPACE", str(tmp_path))

    import infra.config as config_mod

    importlib.reload(config_mod)
    import web.main as web_main

    importlib.reload(web_main)

    from fastapi.testclient import TestClient

    with TestClient(web_main.app) as c:
        yield c


def test_list_chat_sessions_empty_initially(client):
    resp = client.get("/chat-sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}


def test_list_chat_sessions_fallback_title_new_chat(client):
    """Sesi belum punya title (turn pertama belum selesai) → fallback 'New chat'."""
    import asyncio
    import web.main as web_main
    from infra.chat_sessions import ChatSessionStore

    asyncio.run(ChatSessionStore(web_main.db).ensure_created("s1", "pm"))
    resp = client.get("/chat-sessions")
    data = resp.json()
    assert data["sessions"][0]["title"] == "New chat"
    assert data["sessions"][0]["bucket"] == "today"


def test_get_chat_session_turns_empty_for_unknown_session(client):
    resp = client.get("/chat-sessions/unknown-session/turns")
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "unknown-session", "turns": []}


def test_get_chat_session_turns_returns_transcript(client):
    import asyncio
    import web.main as web_main
    from memory.layers import MemoryManager

    async def seed():
        mm = MemoryManager("pm", "s1", web_main.db)
        await mm.append_turn("user", "halo")
        await mm.append_turn("assistant", "hai juga")

    asyncio.run(seed())
    resp = client.get("/chat-sessions/s1/turns")
    data = resp.json()
    assert data["turns"] == [
        {"role": "user", "content": "halo"},
        {"role": "assistant", "content": "hai juga"},
    ]


def test_delete_chat_session_removes_from_list_and_turns(client):
    import asyncio
    import web.main as web_main
    from infra.chat_sessions import ChatSessionStore
    from memory.layers import MemoryManager

    async def seed():
        await ChatSessionStore(web_main.db).ensure_created("s1", "pm")
        await MemoryManager("pm", "s1", web_main.db).append_turn("user", "halo")

    asyncio.run(seed())
    resp = client.delete("/chat-sessions/s1")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert client.get("/chat-sessions").json() == {"sessions": []}
    assert client.get("/chat-sessions/s1/turns").json()["turns"] == []
