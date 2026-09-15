"""Test SessionSandboxContainerStore (§ IMPROVEMENT-Sandbox-Isolation-Parallelization.md
Fase 3, sandbox lifecycle) — CRUD murni DB, tanpa Docker sama sekali."""

from unittest.mock import patch

import pytest

from core.agent_loop import AgentConfig, AgentLoop
from core.llm_client import LLMChunk
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.sandbox_lifecycle import (
    CURRENT_PERSISTENT_SANDBOX,
    SessionSandboxContainerStore,
    effective_persistent_container,
)


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


async def test_create_and_get(db):
    store = SessionSandboxContainerStore(db)
    assert await store.get("sess-1") is None
    await store.create("sess-1", "openclawn-persist-abc", "openclawn-persist-vol-abc")
    row = await store.get("sess-1")
    assert row["container_id"] == "openclawn-persist-abc"
    assert row["volume_name"] == "openclawn-persist-vol-abc"
    assert row["state"] == "running"


async def test_set_state(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    await store.set_state("sess-1", "paused")
    assert (await store.get("sess-1"))["state"] == "paused"


async def test_touch_updates_last_used_at(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    before = (await store.get("sess-1"))["last_used_at"]
    await store.touch("sess-1")
    after = (await store.get("sess-1"))["last_used_at"]
    # SQLite CURRENT_TIMESTAMP punya resolusi detik — hanya pastikan tak error
    # dan baris masih ada dengan timestamp valid (perbandingan waktu asli ada
    # di test_sandbox_reaper.py lewat run_due_once yang mengontrol `now`).
    assert before is not None and after is not None


async def test_delete(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid", "vol")
    await store.delete("sess-1")
    assert await store.get("sess-1") is None


async def test_list_all(db):
    store = SessionSandboxContainerStore(db)
    await store.create("sess-1", "cid-1", "vol-1")
    await store.create("sess-2", "cid-2", "vol-2")
    rows = await store.list_all()
    assert {r["session_id"] for r in rows} == {"sess-1", "sess-2"}


async def test_count_active(db):
    store = SessionSandboxContainerStore(db)
    assert await store.count_active() == 0
    await store.create("sess-1", "cid-1", "vol-1")
    assert await store.count_active() == 1
    await store.create("sess-2", "cid-2", "vol-2")
    assert await store.count_active() == 2
    await store.delete("sess-1")
    assert await store.count_active() == 1


def test_effective_persistent_container_default_none():
    """ContextVar default None → mode ephemeral, perilaku lama tak berubah."""
    assert effective_persistent_container() is None


def test_effective_persistent_container_reads_contextvar():
    token = CURRENT_PERSISTENT_SANDBOX.set("openclawn-persist-xyz")
    try:
        assert effective_persistent_container() == "openclawn-persist-xyz"
    finally:
        CURRENT_PERSISTENT_SANDBOX.reset(token)


# ── Integrasi AgentLoop.run(): restore/resume ContextVar per-turn ──────────────


async def _fake_stream(provider, model, messages, tools=None, max_tokens=4096):
    yield LLMChunk(type="text", text="ok")


async def test_persistent_sandbox_sets_contextvar_for_next_agentloop(db):
    """Turn 1: sandbox_persist_enable sukses (baris DB tertulis langsung).
    Turn 2 (AgentLoop BARU, sesi sama): CURRENT_PERSISTENT_SANDBOX otomatis
    terisi tanpa tool lain dipanggil lagi — pola sama sandbox image (Fase 8.3)."""
    sid = "sess-persist-restore"
    await SessionSandboxContainerStore(db).create(sid, "openclawn-persist-abc", "vol-abc")

    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        captured["container"] = CURRENT_PERSISTENT_SANDBOX.get()
        yield LLMChunk(type="text", text="ok")

    agent = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    agent.llm.stream_with_fallback = fake_stream
    _ = [ev async for ev in agent.run("halo")]

    assert captured["container"] == "openclawn-persist-abc"


async def test_no_persistent_sandbox_leaves_contextvar_unset(db):
    sid = "sess-no-persist"
    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        captured["container"] = CURRENT_PERSISTENT_SANDBOX.get()
        yield LLMChunk(type="text", text="ok")

    agent = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    agent.llm.stream_with_fallback = fake_stream
    _ = [ev async for ev in agent.run("halo")]

    assert captured["container"] is None


async def test_paused_container_auto_resumed_on_next_turn(db):
    """Container `paused` (idle di-pause reaper) → AgentLoop.run() otomatis
    resume_persistent + set_state('running') SEBELUM turn berjalan, transparan
    bagi model (tak perlu tool 'resume' terpisah)."""
    sid = "sess-auto-resume"
    await SessionSandboxContainerStore(db).create(sid, "cid-paused", "vol-paused")
    await SessionSandboxContainerStore(db).set_state(sid, "paused")

    agent = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    agent.llm.stream_with_fallback = _fake_stream

    with patch("tools.sandbox.asyncio.create_subprocess_exec") as mock_exec:
        from unittest.mock import AsyncMock, MagicMock

        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc

        _ = [ev async for ev in agent.run("halo")]

        assert mock_exec.call_args.args[:2] == ("docker", "unpause")

    row = await SessionSandboxContainerStore(db).get(sid)
    assert row["state"] == "running"


async def test_docker_unavailable_during_resume_does_not_crash_turn(db):
    """Audit produksi 2026-09-15: Docker sepenuhnya tak tersedia (binary hilang)
    saat auto-resume container `paused` sebelumnya membuat SandboxUnavailable
    RAISE sebelum try/finally sempat melindungi — turn CRASH TOTAL sebelum LLM
    sempat dipanggil sama sekali, dan baris DB tetap 'paused' selamanya (sesi
    rusak permanen). Sekarang harus fail-safe: turn tetap selesai normal."""
    sid = "sess-docker-down"
    await SessionSandboxContainerStore(db).create(sid, "cid-x", "vol-x")
    await SessionSandboxContainerStore(db).set_state(sid, "paused")

    agent = AgentLoop(AgentConfig(role="dev", session_id=sid), db=db)
    agent.llm.stream_with_fallback = _fake_stream

    with patch(
        "tools.sandbox.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("docker not found"),
    ):
        events = [ev async for ev in agent.run("halo")]

    assert any(ev.type == "token" for ev in events)
    # Sesi jatuh ke jalur ephemeral untuk turn ini (bukan mengklaim resume sukses).
    row = await SessionSandboxContainerStore(db).get(sid)
    assert row["state"] == "paused"
