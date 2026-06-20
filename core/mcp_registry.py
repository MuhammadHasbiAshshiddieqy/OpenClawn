"""Registry server MCP — CRUD definisi server + muat tool-nya ke TOOL_REGISTRY.

Menjembatani definisi server (DB, tabel `mcp_servers`) dengan tool runtime: saat
startup (`load_all`), tiap server enabled di-discover, dan tiap tool-nya dibungkus
`MCPTool` lalu didaftarkan ke `TOOL_REGISTRY` global dengan nama `mcp__<server>__<tool>`.

KEAMANAN (§1): semua MCP tool `requires_approval=True` (lihat MCPTool). Discover
fail-safe: server yang error di-skip, tak menjatuhkan startup. Tool MCP yang sudah
terdaftar dibuang dulu sebelum reload agar idempoten.

Extractable: bergantung DatabaseManager + MCPClient + TOOL_REGISTRY.
"""

import json

from core.mcp_client import MCPClient, MCPServerConfig
from infra.database import DatabaseManager
from infra.logging import log
from tools import TOOL_REGISTRY
from tools.mcp_tool import MCP_PREFIX, MCPTool


class MCPRegistry:
    """Kelola server MCP (DB) + registrasi dinamis tool-nya ke TOOL_REGISTRY."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    # ── CRUD server ────────────────────────────────────────────────────────────

    async def add_server(
        self,
        name: str,
        transport: str,
        command: list[str] | None = None,
        url: str = "",
        env: dict | None = None,
    ) -> dict:
        """Tambah definisi server MCP. Validasi minimal sesuai transport."""
        name = name.strip()
        if not name or transport not in ("stdio", "http"):
            return {"error": "name wajib & transport harus stdio|http"}
        if transport == "stdio" and not command:
            return {"error": "transport stdio butuh command"}
        if transport == "http" and not url.strip():
            return {"error": "transport http butuh url"}
        try:
            await self.db.execute(
                """INSERT INTO mcp_servers (name, transport, command, url, env, enabled)
                   VALUES (?,?,?,?,?,1)""",
                (
                    name,
                    transport,
                    json.dumps(command or []),
                    url.strip(),
                    json.dumps(env or {}),
                ),
            )
        except Exception as e:  # noqa: BLE001 — kemungkinan UNIQUE(name)
            return {"error": f"gagal menambah server (nama mungkin sudah ada): {e}"}
        return {"ok": True, "name": name}

    async def list_servers(self) -> list[dict]:
        return await self.db.fetchall("SELECT * FROM mcp_servers ORDER BY id DESC")

    async def set_enabled(self, server_id: int, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE mcp_servers SET enabled=? WHERE id=?", (1 if enabled else 0, server_id)
        )

    async def delete(self, server_id: int) -> None:
        await self.db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))

    # ── Muat tool ke registry ───────────────────────────────────────────────────

    def _config_from_row(self, row: dict) -> MCPServerConfig:
        try:
            command = json.loads(row.get("command") or "[]")
        except (json.JSONDecodeError, TypeError):
            command = []
        try:
            env = json.loads(row.get("env") or "{}")
        except (json.JSONDecodeError, TypeError):
            env = {}
        return MCPServerConfig(
            name=row["name"],
            transport=row["transport"],
            command=command,
            url=row.get("url") or "",
            env=env,
        )

    @staticmethod
    def _clear_registered() -> None:
        """Buang semua tool MCP dari TOOL_REGISTRY (idempoten sebelum reload)."""
        for key in [k for k in TOOL_REGISTRY if k.startswith(MCP_PREFIX)]:
            del TOOL_REGISTRY[key]

    async def load_all(self) -> dict:
        """Discover & daftarkan tool dari semua server enabled. Fail-safe per server.

        Mengembalikan ringkasan {servers, tools, errors} untuk log/UI.
        """
        self._clear_registered()
        rows = await self.db.fetchall("SELECT * FROM mcp_servers WHERE enabled=1")
        total_tools = 0
        errors: list[str] = []
        for row in rows:
            cfg = self._config_from_row(row)
            try:
                client = MCPClient(cfg)
                specs = await client.list_tools()
            except Exception as e:  # noqa: BLE001 — server eksternal, jangan jatuhkan startup
                errors.append(f"{cfg.name}: {e}")
                log.warning("mcp_load_server_failed", server=cfg.name, error=str(e))
                continue
            for spec in specs:
                tool = MCPTool(spec, client)
                TOOL_REGISTRY[tool.name] = tool
                total_tools += 1
            log.info("mcp_server_loaded", server=cfg.name, tools=len(specs))
        return {"servers": len(rows), "tools": total_tools, "errors": errors}

    async def discovered_tools(self) -> list[dict]:
        """Daftar tool MCP yang saat ini terdaftar (untuk UI /mcp)."""
        return [
            {"name": k, "schema": v.schema()}
            for k, v in sorted(TOOL_REGISTRY.items())
            if k.startswith(MCP_PREFIX)
        ]
