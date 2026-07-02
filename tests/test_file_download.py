"""Test untuk fitur download file yang ditulis agent (§ user request: file harus
bisa diunduh). Dua bagian: (1) AgentLoop memancarkan AgentEvent(type="file_created")
saat tool penulis file sukses, (2) GET /workspace/download menyajikannya dengan aman
(dibatasi ke workspace_root, sama seperti guard yang dipakai tool file_write).
"""

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from core.agent_loop import AgentConfig, AgentLoop
from core.llm_client import LLMChunk
from infra.config import CONFIG, AppConfig
from infra.database import DatabaseManager


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


def _set_workspace(monkeypatch, path):
    """Arahkan workspace_root tool file ke `path` (CONFIG frozen → ganti referensi).

    Sama seperti pola di tests/test_tools.py: tool file (`file_ops.py`) memakai
    `CONFIG` singleton MODULE-LEVEL, bukan config instance yang dipassing ke
    AgentLoop — jadi patch harus di `tools.file_ops.CONFIG`, kalau tidak file
    sungguhan akan ditulis ke workspace_root default (project root nyata).
    """
    patched = dataclasses.replace(CONFIG, workspace_root=str(path))
    monkeypatch.setattr("tools.file_ops.CONFIG", patched)


def _fake_stream_calling_tool(tool_name: str, tool_input: dict):
    """LLM mock: giliran pertama panggil tool, giliran kedua jawab teks (stop)."""
    calls = {"n": 0}

    async def stream(provider, model, messages, tools_schema, max_tokens=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield LLMChunk(type="tool_call", tool_name=tool_name, tool_input=tool_input)
        else:
            yield LLMChunk(type="text", text="selesai")
        yield LLMChunk(type="usage", usage={"input_tokens": 1, "output_tokens": 1})

    return stream


# ── AgentLoop: emisi AgentEvent(type="file_created") ─────────────────────────────


async def test_file_created_event_emitted_on_successful_write(db, tmp_path, monkeypatch):
    """file_write sukses (approval granted) → event file_created dengan path resolved."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-download-1"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool(
            "file_write", {"path": "hello.go", "content": "package main"}
        ),
    ):
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=True)):
            events = [ev async for ev in agent.run("buat file go")]

    file_events = [ev for ev in events if ev.type == "file_created"]
    assert len(file_events) == 1
    assert file_events[0].text.endswith("hello.go")
    assert str(tmp_path) in file_events[0].text
    assert (tmp_path / "hello.go").read_text() == "package main"


async def test_file_created_event_not_emitted_when_approval_denied(db, tmp_path, monkeypatch):
    """Approval ditolak → file tak ditulis → TIDAK ada event file_created (anti false-positive)."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-download-2"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool(
            "file_write", {"path": "hello.go", "content": "package main"}
        ),
    ):
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=False)):
            events = [ev async for ev in agent.run("buat file go")]

    assert not [ev for ev in events if ev.type == "file_created"]
    assert not (tmp_path / "hello.go").exists()


async def test_file_created_event_not_emitted_for_readonly_tools(db, tmp_path, monkeypatch):
    """Tool yang bukan penulis file (mis. grep) tak pernah memicu file_created."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-download-3"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool("grep", {"pattern": "x"}),
    ):
        events = [ev async for ev in agent.run("cari sesuatu")]

    assert not [ev for ev in events if ev.type == "file_created"]


async def test_file_created_not_emitted_on_workspace_violation(db, tmp_path, monkeypatch):
    """Path di luar workspace → file_write gagal (error, bukan ok) → tak ada file_created."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-download-4"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool(
            "file_write", {"path": "../../etc/evil.go", "content": "x"}
        ),
    ):
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=True)):
            events = [ev async for ev in agent.run("buat file di luar workspace")]

    assert not [ev for ev in events if ev.type == "file_created"]


# ── GET /workspace/download ───────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient dengan DB + workspace sementara (pola sama tests/test_web.py)."""
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


def test_download_existing_file_in_workspace(client, tmp_path):
    (tmp_path / "hello.go").write_text("package main")
    resp = client.get("/workspace/download", params={"path": "hello.go"})
    assert resp.status_code == 200
    assert resp.text == "package main"
    assert "hello.go" in resp.headers.get("content-disposition", "")


def test_download_nested_path_in_workspace(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("isi nested")
    resp = client.get("/workspace/download", params={"path": "sub/nested.txt"})
    assert resp.status_code == 200
    assert resp.text == "isi nested"


def test_download_missing_file_returns_404(client):
    resp = client.get("/workspace/download", params={"path": "does-not-exist.txt"})
    assert resp.status_code == 404


def test_download_path_traversal_returns_404_not_file_content(client, tmp_path):
    """`../` keluar workspace → 404, TIDAK membocorkan file di luar workspace."""
    outside = tmp_path.parent / "secret-outside-workspace.txt"
    outside.write_text("rahasia")
    try:
        resp = client.get("/workspace/download", params={"path": "../secret-outside-workspace.txt"})
        assert resp.status_code == 404
        assert "rahasia" not in resp.text
    finally:
        outside.unlink(missing_ok=True)


def test_download_directory_returns_404_not_error(client, tmp_path):
    (tmp_path / "adir").mkdir()
    resp = client.get("/workspace/download", params={"path": "adir"})
    assert resp.status_code == 404


# ── AgentLoop: status "approval" event (§ chat approval UI) ──────────────────
#
# Regresi lama: tool butuh-approval (file_write dll.) SELALU timeout karena tak
# ada tombol Approve/Reject di UI chat sama sekali. Perbaikan: AgentLoop kini
# meng-emit AgentEvent(type="status", text="approval", approval_id=...) SEBELUM
# memanggil ApprovalGate.request() (yang blocking), agar UI py ID untuk kirim
# POST /approve sementara request masih menunggu.


async def test_approval_status_event_emitted_before_blocking_request(db, tmp_path, monkeypatch):
    """Event status approval muncul dengan approval_id valid, sebelum request selesai."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-approval-1"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool(
            "file_write", {"path": "hello.go", "content": "package main"}
        ),
    ):
        with patch.object(agent.approval, "request", new=AsyncMock(return_value=True)) as req:
            events = [ev async for ev in agent.run("buat file go")]

    approval_events = [ev for ev in events if ev.type == "status" and ev.text == "approval"]
    assert len(approval_events) == 1
    assert approval_events[0].approval_id  # ID dibuat, bukan None/kosong
    # ID yang sama harus diteruskan ke ApprovalGate.request (bukan ID lain yang
    # dibuat ulang di dalam _execute_tool — UI dan backend harus sepakat 1 ID).
    req.assert_called_once()
    assert req.call_args.kwargs.get("approval_id") == approval_events[0].approval_id


async def test_no_approval_event_for_readonly_tools(db, tmp_path, monkeypatch):
    """Tool yang tak butuh approval (grep) tak pernah memicu status approval."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-approval-2"), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool("grep", {"pattern": "x"}),
    ):
        events = [ev async for ev in agent.run("cari sesuatu")]

    assert not [ev for ev in events if ev.type == "status" and ev.text == "approval"]


async def test_no_approval_event_in_autopilot_mode(db, tmp_path, monkeypatch):
    """Autopilot: proposal diantri tanpa Future hidup → tak ada status approval (§1/§17)."""
    _set_workspace(monkeypatch, tmp_path)
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-approval-3", autopilot=True), db=db)
    with patch.object(
        agent.llm,
        "stream_with_fallback",
        side_effect=_fake_stream_calling_tool(
            "file_write", {"path": "hello.go", "content": "package main"}
        ),
    ):
        events = [ev async for ev in agent.run("buat file go")]

    assert not [ev for ev in events if ev.type == "status" and ev.text == "approval"]
