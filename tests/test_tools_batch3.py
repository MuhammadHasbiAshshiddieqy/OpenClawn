"""Test tool batch 3: git_status/diff/log (sandbox), todo_write (DB), pdf_write (reportlab).

Sandbox/Docker di-mock (run_shell), DB :memory:. Tidak ada eksekusi nyata di host.
"""

import dataclasses

import pytest
from unittest.mock import AsyncMock

from infra.config import AppConfig
from infra.database import DatabaseManager
from tools import TOOL_REGISTRY
from tools.git import GitDiffTool, GitLogTool, GitStatusTool
from tools.todo import TodoWriteTool
from tools.document import PdfWriteTool


def _patch_workspace(monkeypatch, tmp_path, *mods):
    from infra.config import CONFIG

    patched = dataclasses.replace(CONFIG, workspace_root=str(tmp_path))
    for mod in mods:
        monkeypatch.setattr(f"{mod}.CONFIG", patched)


# ── git tools (read-only via sandbox) ─────────────────────────────────────────


def test_git_tools_are_read_only():
    """git_status/diff/log read-only → tidak butuh approval."""
    for name in ("git_status", "git_diff", "git_log"):
        assert TOOL_REGISTRY[name].requires_approval is False


@pytest.mark.asyncio
async def test_git_status_runs_in_sandbox_not_host():
    """git_status mendelegasikan ke DockerSandbox.run_shell (bukan subprocess host)."""
    tool = GitStatusTool()
    tool.sandbox.run_shell = AsyncMock(return_value={"stdout": "## main", "exit_code": 0})
    await tool.execute({}, vault=None)
    tool.sandbox.run_shell.assert_called_once()
    cmd = tool.sandbox.run_shell.call_args[0][0]
    assert cmd.startswith("git -C /work ")
    assert "status" in cmd


@pytest.mark.asyncio
async def test_git_log_count_clamped():
    """count dijepit ke [1,50] agar output tak membanjiri."""
    tool = GitLogTool()
    tool.sandbox.run_shell = AsyncMock(return_value={"stdout": "", "exit_code": 0})
    await tool.execute({"count": 999}, vault=None)
    cmd = tool.sandbox.run_shell.call_args[0][0]
    assert "-n 50" in cmd  # diklamp dari 999


@pytest.mark.asyncio
async def test_git_diff_path_is_quoted():
    """path di-shlex.quote → tidak bisa menyuntik opsi/perintah git arbitrer."""
    tool = GitDiffTool()
    tool.sandbox.run_shell = AsyncMock(return_value={"stdout": "", "exit_code": 0})
    await tool.execute({"path": "a.py; rm -rf /"}, vault=None)
    cmd = tool.sandbox.run_shell.call_args[0][0]
    # seluruh path berbahaya terbungkus quote → jadi satu argumen literal
    assert "'a.py; rm -rf /'" in cmd


@pytest.mark.asyncio
async def test_git_status_fails_safe_without_docker():
    """Docker absen → error anggun, bukan eksekusi host."""
    from tools.sandbox import SandboxUnavailable

    tool = GitStatusTool()
    tool.sandbox.run_shell = AsyncMock(side_effect=SandboxUnavailable("no docker"))
    result = await tool.execute({}, vault=None)
    assert "error" in result and "host" in result["error"].lower()


@pytest.mark.asyncio
async def test_git_status_reports_non_repo():
    """Workspace bukan repo git → pesan jelas."""
    tool = GitStatusTool()
    tool.sandbox.run_shell = AsyncMock(
        return_value={"stdout": "", "stderr": "fatal: not a git repository", "exit_code": 128}
    )
    result = await tool.execute({}, vault=None)
    assert "error" in result and "git" in result["error"].lower()


# ── todo_write (DB) ───────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await manager.conn()
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_todo_write_persists_list(db):
    """todo_write menyimpan daftar per sesi dengan status."""
    tool = TodoWriteTool()
    result = await tool.execute(
        {
            "_session_id": "s1",
            "todos": [
                {"content": "langkah 1", "status": "completed"},
                {"content": "langkah 2", "status": "in_progress"},
            ],
        },
        vault=None,
        db=db,
    )
    assert result["ok"] is True
    assert result["total"] == 2
    rows = await db.fetchall(
        "SELECT content, status, position FROM agent_todos WHERE session_id='s1' ORDER BY position"
    )
    assert rows[0]["content"] == "langkah 1"
    assert rows[1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_todo_write_replaces_previous_snapshot(db):
    """Panggilan kedua mengganti seluruh daftar (snapshot), bukan menambah."""
    tool = TodoWriteTool()
    await tool.execute(
        {"_session_id": "s2", "todos": [{"content": "lama", "status": "pending"}]},
        vault=None,
        db=db,
    )
    await tool.execute(
        {"_session_id": "s2", "todos": [{"content": "baru", "status": "pending"}]},
        vault=None,
        db=db,
    )
    rows = await db.fetchall("SELECT content FROM agent_todos WHERE session_id='s2'")
    assert len(rows) == 1
    assert rows[0]["content"] == "baru"


@pytest.mark.asyncio
async def test_todo_write_rejects_bad_status(db):
    """Status tak valid → error, tidak menulis."""
    tool = TodoWriteTool()
    result = await tool.execute(
        {"_session_id": "s3", "todos": [{"content": "x", "status": "ngawur"}]},
        vault=None,
        db=db,
    )
    assert "error" in result
    rows = await db.fetchall("SELECT * FROM agent_todos WHERE session_id='s3'")
    assert rows == []


@pytest.mark.asyncio
async def test_todo_write_requires_session(db):
    """Tanpa _session_id (tak disuntik AgentLoop) → error."""
    tool = TodoWriteTool()
    result = await tool.execute(
        {"todos": [{"content": "x", "status": "pending"}]}, vault=None, db=db
    )
    assert "error" in result


def test_todo_write_no_approval():
    """todo_write menulis ke tabel internal, bukan filesystem → tanpa approval."""
    assert TOOL_REGISTRY["todo_write"].requires_approval is False


# ── pdf_write (reportlab) ─────────────────────────────────────────────────────


def test_pdf_write_requires_approval():
    """pdf_write menulis file → butuh approval."""
    assert TOOL_REGISTRY["pdf_write"].requires_approval is True


@pytest.mark.asyncio
async def test_pdf_write_produces_pdf(tmp_path, monkeypatch):
    """pdf_write menghasilkan file PDF nyata (header %PDF)."""
    _patch_workspace(monkeypatch, tmp_path, "tools.document")
    content = {
        "title": "Laporan",
        "sections": [{"heading": "Ringkasan", "body": "isi", "bullets": ["a", "b"]}],
    }
    result = await PdfWriteTool().execute({"path": "out.pdf", "content": content}, vault=None)
    assert result.get("ok") is True
    data = (tmp_path / "out.pdf").read_bytes()
    assert data[:4] == b"%PDF"


@pytest.mark.asyncio
async def test_pdf_write_rejects_outside_workspace(tmp_path, monkeypatch):
    """Path di luar workspace ditolak (keamanan #1)."""
    _patch_workspace(monkeypatch, tmp_path, "tools.document")
    result = await PdfWriteTool().execute(
        {"path": "../escape.pdf", "content": {"title": "x"}}, vault=None
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_pdf_write_rejects_bad_content(tmp_path, monkeypatch):
    """content bukan objek → error, tidak crash."""
    _patch_workspace(monkeypatch, tmp_path, "tools.document")
    result = await PdfWriteTool().execute({"path": "x.pdf", "content": "string"}, vault=None)
    assert "error" in result
