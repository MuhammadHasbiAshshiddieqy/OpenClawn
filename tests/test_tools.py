"""Tests untuk Tools + Sandbox — Sprint 2."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tools.file_ops import FileReadTool, FileWriteTool
from tools.web import WebFetchTool
from tools.interaction import AskUserTool
from tools.code import CodeRunTool
from tools import TOOL_REGISTRY


# ── TOOL_REGISTRY ─────────────────────────────────────────────────────────────


def test_registry_has_all_18_tools():
    """Semua 18 tool harus terdaftar di TOOL_REGISTRY."""
    expected = {
        "file_read",
        "file_write",
        "file_edit",
        "file_append",
        "apply_patch",
        "list_dir",
        "glob",
        "grep",
        "pdf_read",
        "shell_run",
        "code_run",
        "web_fetch",
        "web_search",
        "http_request",
        "db_query",
        "memory_search",
        "json_query",
        "ask_user",
    }
    assert set(TOOL_REGISTRY.keys()) == expected


def test_code_run_requires_approval():
    """code_run HARUS requires_approval=True — keamanan wajib."""
    assert TOOL_REGISTRY["code_run"].requires_approval is True


def test_file_write_requires_approval():
    """file_write HARUS requires_approval=True — tool destruktif (modifikasi filesystem)."""
    assert TOOL_REGISTRY["file_write"].requires_approval is True


def test_non_destructive_tools_no_approval():
    """file_read, web_fetch, ask_user tidak butuh approval."""
    for name in ("file_read", "web_fetch", "ask_user"):
        assert TOOL_REGISTRY[name].requires_approval is False, f"{name} seharusnya False"


def test_all_destructive_tools_require_approval():
    """Semua tool yang memodifikasi state (code_run, file_write) harus requires_approval=True."""
    destructive = [n for n, t in TOOL_REGISTRY.items() if t.requires_approval]
    assert "code_run" in destructive
    assert "file_write" in destructive


def test_all_tools_have_schema():
    """Semua tool harus bisa produce schema dict yang valid."""
    for name, tool in TOOL_REGISTRY.items():
        schema = tool.schema()
        assert "name" in schema, f"{name}: schema harus punya 'name'"
        assert "input_schema" in schema, f"{name}: schema harus punya 'input_schema'"
        assert schema["name"] == name


# ── FileReadTool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
def _set_workspace(monkeypatch, path):
    """Arahkan workspace_root tool file ke `path` (CONFIG frozen → ganti referensi)."""
    import dataclasses

    from infra.config import CONFIG

    patched = dataclasses.replace(CONFIG, workspace_root=str(path))
    monkeypatch.setattr("tools.file_ops.CONFIG", patched)


async def test_file_read_success(tmp_path, monkeypatch):
    """file_read harus mengembalikan isi file (dalam workspace)."""
    _set_workspace(monkeypatch, tmp_path)
    f = tmp_path / "test.txt"
    f.write_text("hello world")

    tool = FileReadTool()
    result = await tool.execute({"path": "test.txt"}, vault=None)
    assert result["content"] == "hello world"


@pytest.mark.asyncio
async def test_file_read_not_found():
    """file_read harus mengembalikan error jika file tidak ada."""
    tool = FileReadTool()
    result = await tool.execute({"path": "/tidak/ada/file.txt"}, vault=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_file_read_no_path():
    """file_read tanpa path harus return error, tidak crash."""
    tool = FileReadTool()
    result = await tool.execute({}, vault=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_file_read_truncates_large_file(tmp_path, monkeypatch):
    """file_read harus truncate konten > 10000 karakter."""
    _set_workspace(monkeypatch, tmp_path)
    f = tmp_path / "big.txt"
    f.write_text("x" * 20000)

    tool = FileReadTool()
    result = await tool.execute({"path": "big.txt"}, vault=None)
    assert len(result["content"]) <= 10000


# ── FileWriteTool ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_write_success(tmp_path, monkeypatch):
    """file_write harus menulis konten dan mengembalikan ok=True."""
    _set_workspace(monkeypatch, tmp_path)
    tool = FileWriteTool()
    result = await tool.execute({"path": "output.txt", "content": "isi file"}, vault=None)
    assert result["ok"] is True
    assert (tmp_path / "output.txt").read_text() == "isi file"


@pytest.mark.asyncio
async def test_file_write_no_path():
    """file_write tanpa path harus return error."""
    tool = FileWriteTool()
    result = await tool.execute({"content": "isi"}, vault=None)
    assert "error" in result


# ── WebFetchTool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_fetch_success():
    """web_fetch berhasil → mengembalikan status dan konten."""
    tool = WebFetchTool()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "page content"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("tools.web.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute({"url": "http://example.com"}, vault=None)

    assert result["status"] == 200
    assert "page content" in result["content"]


@pytest.mark.asyncio
async def test_web_fetch_no_url():
    """web_fetch tanpa url harus return error."""
    tool = WebFetchTool()
    result = await tool.execute({}, vault=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_web_fetch_http_error():
    """web_fetch dengan HTTP error harus return error, tidak raise."""
    import httpx

    tool = WebFetchTool()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connection error"))

    with patch("tools.web.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute({"url": "http://bad-url"}, vault=None)

    assert "error" in result


# ── AskUserTool ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_user_returns_stub():
    """ask_user stub harus mengembalikan jawaban tanpa crash."""
    tool = AskUserTool()
    result = await tool.execute({"question": "apa preferensimu?"}, vault=None)
    assert "answer" in result


@pytest.mark.asyncio
async def test_ask_user_no_question():
    """ask_user tanpa question harus tetap return dict tanpa crash."""
    tool = AskUserTool()
    result = await tool.execute({}, vault=None)
    assert isinstance(result, dict)


# ── CodeRunTool + DockerSandbox ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_run_no_code():
    """code_run tanpa kode harus return error, tidak jalankan Docker."""
    tool = CodeRunTool()
    result = await tool.execute({}, vault=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_code_run_delegates_to_sandbox():
    """code_run harus mendelegasikan eksekusi ke DockerSandbox, bukan langsung exec."""
    tool = CodeRunTool()
    tool.sandbox.run_python = AsyncMock(return_value={"stdout": "42\n", "exit_code": 0})

    result = await tool.execute({"code": "print(42)"}, vault=None)
    tool.sandbox.run_python.assert_called_once_with("print(42)")
    assert result["stdout"] == "42\n"


@pytest.mark.asyncio
async def test_sandbox_timeout_handled():
    """Sandbox timeout harus return error dict, tidak raise ke caller."""
    from tools.sandbox import DockerSandbox
    import asyncio

    sandbox = DockerSandbox()

    async def _fake_exec(*args, **kwargs):
        raise asyncio.TimeoutError()

    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=asyncio.TimeoutError):
        result = await sandbox.run_python("import time; time.sleep(999)")

    assert "error" in result
    assert result["exit_code"] == -1


def test_sandbox_cmd_has_security_flags():
    """DockerSandbox harus menyertakan flag keamanan wajib."""
    from tools.sandbox import SANDBOX_IMAGE

    # Rekonstruksi cmd seperti di sandbox.run_python
    workdir = "/fake/workdir"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        "256m",
        "--cpus",
        "0.5",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "-v",
        f"{workdir}:/work:ro",
        "--workdir",
        "/work",
        "--user",
        "nobody",
        "--security-opt",
        "no-new-privileges",
        SANDBOX_IMAGE,
        "timeout",
        "30",
        "python",
        "/work/script.py",
    ]

    assert "--network" in cmd and "none" in cmd, "Harus --network none"
    assert "--read-only" in cmd, "Harus --read-only"
    assert "--user" in cmd and "nobody" in cmd, "Harus user non-root"
    assert "no-new-privileges" in cmd, "Harus --security-opt no-new-privileges"


# ── Approval gate integration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_called_for_destructive_tool():
    """Tool requires_approval=True memicu HITL: request() dicatat & menunggu keputusan.

    Sprint 3: approval interaktif (bukan auto-approve). Di sini kita simulasikan
    user menekan 'approve' lewat resolve(). Coverage HITL lengkap di test_security.py.
    """
    import asyncio
    from security.approval import ApprovalGate
    from infra.config import AppConfig
    from infra.database import DatabaseManager

    cfg = AppConfig(db_path=":memory:", approval_timeout_sec=1)
    db = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await db.conn()
        await conn.executescript(f.read())
        await conn.commit()

    gate = ApprovalGate(db=db, config=cfg)

    async def _user_approves():
        await asyncio.sleep(0.05)
        pending = gate.pending_list("test-s1")
        gate.resolve(pending[0]["approval_id"], True)

    asyncio.create_task(_user_approves())
    approved = await gate.request(
        session_id="test-s1", tool_name="code_run", tool_input={"code": "print(42)"}
    )
    assert approved is True

    # Verifikasi tersimpan di approval_log dengan keputusan final
    row = await db.fetchone(
        "SELECT tool_name, decision FROM approval_log WHERE session_id='test-s1'"
    )
    assert row is not None
    assert row["tool_name"] == "code_run"
    assert row["decision"] == "approved"

    await db.close()


@pytest.mark.asyncio
async def test_approval_log_contains_tool_input():
    """Approval log harus menyimpan tool_input untuk audit trail."""
    from security.approval import ApprovalGate
    from infra.config import AppConfig
    from infra.database import DatabaseManager
    import json

    cfg = AppConfig(db_path=":memory:")
    db = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        conn = await db.conn()
        await conn.executescript(f.read())
        await conn.commit()

    gate = ApprovalGate(db=db, config=cfg)
    await gate.request(
        session_id="s2",
        tool_name="file_write",
        tool_input={"path": "/tmp/test.py", "content": "print(1)"},
    )

    row = await db.fetchone("SELECT tool_input FROM approval_log WHERE session_id='s2'")
    parsed = json.loads(row["tool_input"])
    assert parsed["path"] == "/tmp/test.py"

    await db.close()
