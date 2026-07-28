"""Test MCP: client (SDK di-mock), wrapper Tool approval-gated, SSRF remote,
registry CRUD + load/register, izin wildcard di soul. Tanpa server MCP nyata."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.mcp_client import MCPClient, MCPServerConfig, MCPToolSpec, _extract_text
from core.mcp_registry import MCPRegistry
from infra.config import AppConfig
from infra.database import DatabaseManager
from tools import TOOL_REGISTRY
from tools.mcp_tool import MCPTool, mcp_tool_name


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


# ── MCPTool wrapper (keamanan) ────────────────────────────────────────────────


def test_mcp_tool_always_requires_approval():
    """§1: tool dari server eksternal SELALU butuh approval — tak bisa di-override."""
    spec = MCPToolSpec("github", "create_issue", "buat issue", {"type": "object"})
    tool = MCPTool(spec, MCPClient(MCPServerConfig("github", "stdio", command=["x"])))
    assert tool.requires_approval is True


def test_mcp_tool_name_prefixed():
    assert mcp_tool_name("github", "create_issue") == "mcp__github__create_issue"


def test_mcp_tool_schema_from_server():
    spec = MCPToolSpec("fs", "read_file", "baca file", {"type": "object", "properties": {"p": {}}})
    tool = MCPTool(spec, MCPClient(MCPServerConfig("fs", "stdio", command=["x"])))
    sch = tool.schema()
    assert sch["name"] == "mcp__fs__read_file"
    assert "MCP:fs" in sch["description"]
    assert sch["input_schema"]["properties"] == {"p": {}}


async def test_mcp_tool_execute_strips_internal_fields():
    """Field internal (_session_id) tak diteruskan ke server MCP."""
    spec = MCPToolSpec("fs", "read", "", {"type": "object"})
    client = MCPClient(MCPServerConfig("fs", "stdio", command=["x"]))
    client.call_tool = AsyncMock(return_value={"content": "ok"})
    tool = MCPTool(spec, client)
    await tool.execute({"path": "a.txt", "_session_id": "s1", "_role": "dev"}, vault=None)
    client.call_tool.assert_awaited_once_with("read", {"path": "a.txt"})


# ── MCPClient: SSRF remote + fail-safe ────────────────────────────────────────


async def test_http_transport_blocks_internal_host():
    """Server MCP remote ke host internal ditolak SSRF SEBELUM konek (§1)."""
    client = MCPClient(MCPServerConfig("evil", "http", url="http://localhost:9000/mcp"))
    # list_tools fail-safe → [] (error di-log), call_tool → {"error"}.
    assert await client.list_tools() == []
    res = await client.call_tool("x", {})
    assert "error" in res and "SSRF" in res["error"]


async def test_call_tool_failsafe_on_exception():
    """Exception apa pun di sesi → {"error"}, tak meledak."""
    client = MCPClient(MCPServerConfig("s", "stdio", command=["nonexistent-binary-xyz"]))
    res = await client.call_tool("t", {})
    assert "error" in res


async def test_list_tools_failsafe_returns_empty():
    client = MCPClient(MCPServerConfig("s", "stdio", command=["nonexistent-binary-xyz"]))
    assert await client.list_tools() == []


def test_extract_text_from_content_blocks():
    class Block:
        def __init__(self, t, txt):
            self.type, self.text = t, txt

    assert _extract_text([Block("text", "halo"), Block("image", "x"), Block("text", "dunia")]) == (
        "halo\ndunia"
    )


# ── MCPRegistry: CRUD + load/register ─────────────────────────────────────────


async def test_add_and_list_server(db):
    reg = MCPRegistry(db)
    res = await reg.add_server("fs", "stdio", command=["npx", "server-fs"])
    assert res["ok"] is True
    rows = await reg.list_servers()
    assert len(rows) == 1 and rows[0]["name"] == "fs"


async def test_add_server_validates_transport(db):
    reg = MCPRegistry(db)
    assert "error" in await reg.add_server("x", "carrier-pigeon")
    assert "error" in await reg.add_server("x", "stdio")  # tanpa command
    assert "error" in await reg.add_server("x", "http")  # tanpa url


async def test_toggle_and_delete_server(db):
    reg = MCPRegistry(db)
    await reg.add_server("fs", "stdio", command=["x"])
    sid = (await reg.list_servers())[0]["id"]
    await reg.set_enabled(sid, False)
    assert (await reg.list_servers())[0]["enabled"] == 0
    await reg.delete(sid)
    assert await reg.list_servers() == []


async def test_load_registers_discovered_tools(db):
    """load_all men-discover tool & mendaftarkannya ke TOOL_REGISTRY dengan prefix."""
    reg = MCPRegistry(db)
    await reg.add_server("fs", "stdio", command=["x"])

    specs = [MCPToolSpec("fs", "read_file", "baca", {"type": "object"})]
    with patch.object(MCPClient, "list_tools", new=AsyncMock(return_value=specs)):
        summary = await reg.load_all()
    assert summary["tools"] == 1
    assert "mcp__fs__read_file" in TOOL_REGISTRY
    assert TOOL_REGISTRY["mcp__fs__read_file"].requires_approval is True
    # cleanup agar tak bocor ke test lain
    del TOOL_REGISTRY["mcp__fs__read_file"]


async def test_load_failsafe_on_bad_server(db):
    """Server yang gagal discover di-skip, startup tak jatuh, error dilaporkan."""
    reg = MCPRegistry(db)
    await reg.add_server("broken", "stdio", command=["nonexistent-binary-xyz"])
    summary = await reg.load_all()
    assert summary["tools"] == 0  # tak ada tool, tapi tak crash


async def test_env_encrypted_at_rest(db):
    """Audit produksi 2026-07-28: mcp_servers.env dienkripsi, tak plaintext di DB."""
    with patch.dict("os.environ", {"OPENCLAWN_ENCRYPTION_KEY": "kunci-uji-panjang"}):
        reg = MCPRegistry(db)
        res = await reg.add_server(
            "gh", "stdio", command=["x"], env={"GITHUB_TOKEN": "ghp_rahasia123456"}
        )
        assert res["ok"] is True

        row = await db.fetchone("SELECT env FROM mcp_servers WHERE name='gh'")
        assert "ghp_rahasia123456" not in row["env"]  # bukan plaintext di kolom DB

        # list_servers() tetap tak pernah mengembalikan nilai mentah (terenkripsi
        # ATAU plaintext) — hanya has_env.
        listed = await reg.list_servers()
        assert "env" not in listed[0]
        assert listed[0]["has_env"] is True

        # tapi _config_from_row (dipakai load_all utk konek ke server sungguhan)
        # berhasil mendekripsi kembali nilai aslinya.
        cfg = reg._config_from_row(await db.fetchone("SELECT * FROM mcp_servers WHERE name='gh'"))
        assert cfg.env == {"GITHUB_TOKEN": "ghp_rahasia123456"}


async def test_add_server_without_key_fails_when_env_given(db):
    """Tanpa OPENCLAWN_ENCRYPTION_KEY, add_server dengan env harus gagal loud —
    bukan diam-diam simpan plaintext (CLAUDE.md §1.2)."""
    with patch.dict("os.environ", {}, clear=True):
        reg = MCPRegistry(db)
        res = await reg.add_server("gh", "stdio", command=["x"], env={"TOKEN": "x"})
        assert "error" in res
        assert await reg.list_servers() == []  # tak ada baris tertulis


async def test_add_server_without_env_needs_no_key(db):
    """Server tanpa env sama sekali tetap jalan tanpa OPENCLAWN_ENCRYPTION_KEY."""
    with patch.dict("os.environ", {}, clear=True):
        reg = MCPRegistry(db)
        res = await reg.add_server("fs", "stdio", command=["x"])
        assert res["ok"] is True
        assert (await reg.list_servers())[0]["has_env"] is False


async def test_legacy_plaintext_env_still_readable(db):
    """Baris LAMA pra-enkripsi (env plaintext JSON, dari sebelum fitur ini ada)
    harus tetap terbaca — fallback dekripsi ke parse plaintext (backward-compat)."""
    await db.execute(
        """INSERT INTO mcp_servers (name, transport, command, url, env, enabled)
           VALUES (?,?,?,?,?,1)""",
        ("legacy", "stdio", json.dumps(["x"]), "", json.dumps({"OLD_TOKEN": "abc"})),
    )
    reg = MCPRegistry(db)
    with patch.dict("os.environ", {"OPENCLAWN_ENCRYPTION_KEY": "kunci-uji"}):
        cfg = reg._config_from_row(
            await db.fetchone("SELECT * FROM mcp_servers WHERE name='legacy'")
        )
    assert cfg.env == {"OLD_TOKEN": "abc"}


async def test_load_idempotent_clears_old(db):
    """Reload membuang tool MCP lama dulu (idempoten)."""
    reg = MCPRegistry(db)
    await reg.add_server("fs", "stdio", command=["x"])
    specs = [MCPToolSpec("fs", "a", "", {"type": "object"})]
    with patch.object(MCPClient, "list_tools", new=AsyncMock(return_value=specs)):
        await reg.load_all()
        await reg.load_all()  # kedua kali tak menggandakan
    mcp_keys = [k for k in TOOL_REGISTRY if k.startswith("mcp__")]
    assert mcp_keys == ["mcp__fs__a"]
    del TOOL_REGISTRY["mcp__fs__a"]


# ── Izin wildcard di soul ─────────────────────────────────────────────────────


def test_soul_wildcard_allows_mcp():
    """soul.toml dengan 'mcp__*' mengizinkan semua MCP tool; tanpa itu → ditolak."""
    from core.agent_loop import AgentLoop

    # _tool_allowed murni fungsi dari self._soul → bisa diuji tanpa init penuh.
    agent = AgentLoop.__new__(AgentLoop)
    agent._soul = {"tools": {"allowed": ["file_read", "mcp__github__*"]}}
    # github tool diizinkan via wildcard server; gitlab tidak.
    assert agent._tool_allowed("mcp__github__create_issue") is True
    assert agent._tool_allowed("mcp__gitlab__create_issue") is False
    assert agent._tool_allowed("file_read") is True
    # global wildcard
    agent._soul = {"tools": {"allowed": ["mcp__*"]}}
    assert agent._tool_allowed("mcp__anything__tool") is True
    # tanpa wildcard → MCP ditolak (opt-in §1)
    agent._soul = {"tools": {"allowed": ["file_read"]}}
    assert agent._tool_allowed("mcp__github__x") is False
