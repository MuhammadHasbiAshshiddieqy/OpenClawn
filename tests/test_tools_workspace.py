"""Test tool baru (glob/grep/file_edit/web_search/http_request) + workspace guard.

Semua tool file dibatasi ke workspace_root. Test memakai tmp_path sebagai workspace
dan mem-monkeypatch CONFIG.workspace_root. LLM/jaringan tidak pernah dipanggil nyata.
"""

import dataclasses

import pytest
from unittest.mock import AsyncMock

from infra.config import CONFIG
from infra.workspace import WorkspaceViolation, resolve_in_workspace
from tools.file_ops import FileEditTool, FileReadTool, FileWriteTool
from tools.search import GlobTool, GrepTool
from tools.shell import ListDirTool
from tools.web import HttpRequestTool, WebSearchTool


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Arahkan workspace_root ke tmp_path. CONFIG frozen → ganti referensi CONFIG
    di tiap modul tool dengan salinan yang workspace_root-nya tmp_path."""
    patched = dataclasses.replace(CONFIG, workspace_root=str(tmp_path))
    for mod in ("tools.file_ops", "tools.search", "tools.shell"):
        monkeypatch.setattr(f"{mod}.CONFIG", patched)
    return tmp_path


# ── Workspace guard ───────────────────────────────────────────────────────────


def test_guard_allows_inside(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    resolved = resolve_in_workspace("a.txt", str(tmp_path))
    assert resolved == (tmp_path / "a.txt").resolve()


def test_guard_blocks_traversal(tmp_path):
    with pytest.raises(WorkspaceViolation):
        resolve_in_workspace("../../etc/passwd", str(tmp_path))


def test_guard_blocks_absolute_outside(tmp_path):
    with pytest.raises(WorkspaceViolation):
        resolve_in_workspace("/etc/passwd", str(tmp_path))


def test_guard_blocks_symlink_escape(tmp_path):
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(WorkspaceViolation):
        resolve_in_workspace("link/passwd", str(tmp_path))


# ── file tools respect workspace ────────────────────────────────────────────────


async def test_file_read_blocks_outside(ws):
    res = await FileReadTool().execute({"path": "../../../etc/passwd"}, vault=None)
    assert "error" in res and "workspace" in res["error"].lower()


async def test_file_write_then_read_roundtrip(ws):
    w = await FileWriteTool().execute({"path": "out/x.txt", "content": "halo"}, vault=None)
    assert w["ok"] is True
    r = await FileReadTool().execute({"path": "out/x.txt"}, vault=None)
    assert r["content"] == "halo"


async def test_list_dir_blocks_outside(ws):
    res = await ListDirTool().execute({"path": "../.."}, vault=None)
    assert "error" in res


# ── glob ────────────────────────────────────────────────────────────────────────


async def test_glob_finds_files(ws):
    (ws / "a.py").write_text("x")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("y")
    res = await GlobTool().execute({"pattern": "**/*.py"}, vault=None)
    assert res["count"] == 2
    assert "a.py" in res["matches"]


async def test_glob_skips_git_dir(ws):
    (ws / ".git").mkdir()
    (ws / ".git" / "config.py").write_text("x")
    (ws / "real.py").write_text("y")
    res = await GlobTool().execute({"pattern": "**/*.py"}, vault=None)
    assert res["matches"] == ["real.py"]


# ── grep ────────────────────────────────────────────────────────────────────────


async def test_grep_finds_match(ws):
    (ws / "a.txt").write_text("foo\nbar TODO baz\n")
    res = await GrepTool().execute({"pattern": "TODO"}, vault=None)
    assert res["count"] == 1
    assert res["matches"][0]["line"] == 2


async def test_grep_invalid_regex(ws):
    res = await GrepTool().execute({"pattern": "[unclosed"}, vault=None)
    assert "error" in res


# ── file_edit ─────────────────────────────────────────────────────────────────


async def test_file_edit_replaces(ws):
    (ws / "c.py").write_text("a = 1\nb = 2\n")
    res = await FileEditTool().execute(
        {"path": "c.py", "old_string": "a = 1", "new_string": "a = 99"}, vault=None
    )
    assert res["ok"] is True
    assert (ws / "c.py").read_text() == "a = 99\nb = 2\n"


async def test_file_edit_non_unique_without_replace_all(ws):
    (ws / "d.py").write_text("x\nx\n")
    res = await FileEditTool().execute(
        {"path": "d.py", "old_string": "x", "new_string": "y"}, vault=None
    )
    assert "error" in res and "unik" in res["error"]


async def test_file_edit_missing_string(ws):
    (ws / "e.py").write_text("hello")
    res = await FileEditTool().execute(
        {"path": "e.py", "old_string": "absent", "new_string": "z"}, vault=None
    )
    assert "error" in res


# ── web_search / http_request (vault & network mocked) ──────────────────────────


async def test_web_search_missing_key_graceful():
    vault = AsyncMock()
    vault.get.side_effect = ValueError("not found")
    res = await WebSearchTool().execute({"query": "apa itu fastapi"}, vault=vault)
    assert "error" in res and "TAVILY_API_KEY" in res["error"]


async def test_http_request_rejects_bad_url():
    res = await HttpRequestTool().execute({"url": "ftp://x"}, vault=AsyncMock())
    assert "error" in res


async def test_http_request_rejects_bad_method():
    res = await HttpRequestTool().execute(
        {"url": "https://x.com", "method": "TRACE"}, vault=AsyncMock()
    )
    assert "error" in res
