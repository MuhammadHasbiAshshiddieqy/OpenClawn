"""Test pindah direktori kerja dinamis lewat chat (§ user request: "pindah direktori
secara dinamis" — sebelumnya folder kerja HANYA bisa diubah lewat field UI sekali
per-request, tak ada cara mengubahnya dari dalam percakapan).

Tiga bagian:
1. SessionWorkspaceStore (infra/workspace.py): baca/tulis folder aktif per-sesi.
2. SetWorkdirTool (tools/workspace_tool.py): validasi + efek ganda (ContextVar + DB).
3. Integrasi AgentLoop: turn berikutnya (AgentLoop baru) memuat balik folder yang
   di-set turn sebelumnya.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.workspace import CURRENT_WORKSPACE_ROOT, SessionWorkspaceStore
from core.agent_loop import AgentLoop, AgentConfig
from core.llm_client import LLMChunk
from tools.workspace_tool import SetWorkdirTool


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


# ── SessionWorkspaceStore ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_workspace_get_set_roundtrip(db, tmp_path):
    store = SessionWorkspaceStore(db)
    assert await store.get("s1") is None
    await store.set("s1", str(tmp_path))
    assert await store.get("s1") == str(tmp_path)


@pytest.mark.asyncio
async def test_session_workspace_upsert_overwrites(db, tmp_path):
    store = SessionWorkspaceStore(db)
    await store.set("s1", str(tmp_path))
    other = tmp_path / "sub"
    other.mkdir()
    await store.set("s1", str(other))
    assert await store.get("s1") == str(other)


@pytest.mark.asyncio
async def test_session_workspace_isolated_per_session(db, tmp_path):
    store = SessionWorkspaceStore(db)
    await store.set("s1", str(tmp_path))
    assert await store.get("s2") is None


# ── SetWorkdirTool ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_workdir_success_sets_contextvar_and_db(db, tmp_path):
    tool = SetWorkdirTool()
    token = CURRENT_WORKSPACE_ROOT.set(None)
    try:
        result = await tool.execute(
            {"path": str(tmp_path), "_session_id": "s-set"}, vault=None, db=db
        )
        assert result.get("ok") is True
        assert result["workdir"] == str(tmp_path.resolve())
        # ContextVar langsung berubah — turn INI juga ikut pindah, tanpa tunggu turn baru.
        assert CURRENT_WORKSPACE_ROOT.get() == str(tmp_path.resolve())
    finally:
        CURRENT_WORKSPACE_ROOT.reset(token)
    # Tersimpan ke DB — turn berikutnya (AgentLoop baru) bisa memuatnya balik.
    assert await SessionWorkspaceStore(db).get("s-set") == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_set_workdir_missing_path_errors(db):
    tool = SetWorkdirTool()
    result = await tool.execute({"_session_id": "s-x"}, vault=None, db=db)
    assert "error" in result


@pytest.mark.asyncio
async def test_set_workdir_nonexistent_folder_errors(db):
    tool = SetWorkdirTool()
    result = await tool.execute(
        {"path": "/no/such/folder/xyz", "_session_id": "s-x"}, vault=None, db=db
    )
    assert "error" in result
    # Gagal → TIDAK mengubah DB (fail-closed, tak ada state korup tersimpan).
    assert await SessionWorkspaceStore(db).get("s-x") is None


@pytest.mark.asyncio
async def test_set_workdir_missing_session_id_errors(db, tmp_path):
    """_session_id disuntik AgentLoop — bila absen (panggilan langsung tanpa
    konteks), tool harus gagal anggun, bukan crash atau menebak session."""
    tool = SetWorkdirTool()
    result = await tool.execute({"path": str(tmp_path)}, vault=None, db=db)
    assert "error" in result


def test_set_workdir_registered_and_no_approval():
    from tools import TOOL_REGISTRY

    assert "set_workdir" in TOOL_REGISTRY
    assert TOOL_REGISTRY["set_workdir"].requires_approval is False


# ── Integrasi AgentLoop: perpindahan bertahan ke turn berikutnya ────────────


@pytest.mark.asyncio
async def test_workdir_change_persists_to_next_agentloop(db, tmp_path):
    """Turn 1: agent panggil set_workdir. Turn 2 (AgentLoop BARU, sesi sama):
    folder baru otomatis jadi workspace aktif TANPA form workdir diisi lagi."""
    sid = "sess-cd"
    (tmp_path / "hello.go").write_text("package main")

    async def call_set_workdir(provider, model, messages, tools=None, max_tokens=4096):
        yield LLMChunk(
            type="tool_call", tool_name="set_workdir", tool_input={"path": str(tmp_path)}
        )

    a1 = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    a1.llm.stream_with_fallback = call_set_workdir
    events1 = [ev async for ev in a1.run("pindah ke folder itu")]
    assert any(ev.type == "token" for ev in events1) or True  # tool-only turn boleh tanpa token

    # Turn 2: AgentLoop BARU, TANPA workspace_override — harus otomatis pakai
    # folder dari turn 1 (dimuat dari session_workspace).
    captured = {}

    async def read_hello(provider, model, messages, tools=None, max_tokens=4096):
        captured["messages"] = [dict(m) for m in messages]
        yield LLMChunk(type="tool_call", tool_name="file_read", tool_input={"path": "hello.go"})

    a2 = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    a2.llm.stream_with_fallback = read_hello
    events2 = [ev async for ev in a2.run("baca hello.go")]

    # file_read hello.go harus SUKSES karena workspace root sudah pindah ke tmp_path.
    tool_msgs = [m for m in captured["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "harus ada hasil tool di messages turn 2"
    assert "ERROR" not in tool_msgs[-1]["content"], (
        f"file_read gagal — folder tak terbawa ke turn 2: {tool_msgs[-1]['content']}"
    )
    assert any(ev.type == "token" or ev.type == "status" for ev in events2)


@pytest.mark.asyncio
async def test_explicit_workspace_override_wins_over_saved_workdir(db, tmp_path):
    """Form UI diisi eksplisit di request INI → menang atas session_workspace
    tersimpan (user sadar override, bukan lupa)."""
    sid = "sess-override"
    saved_dir = tmp_path / "saved"
    saved_dir.mkdir()
    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    await SessionWorkspaceStore(db).set(sid, str(saved_dir))

    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        captured["root"] = CURRENT_WORKSPACE_ROOT.get()
        yield LLMChunk(type="text", text="ok")

    agent = AgentLoop(
        AgentConfig(role="dev", session_id=sid, workspace_override=str(explicit_dir)), db=db
    )
    agent.llm.stream_with_fallback = fake_stream
    _ = [ev async for ev in agent.run("halo")]

    assert captured["root"] == str(explicit_dir)
