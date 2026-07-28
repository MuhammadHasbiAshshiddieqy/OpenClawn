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

from cryptography.fernet import InvalidToken

from core.mcp_client import MCPClient, MCPServerConfig
from infra.database import DatabaseManager
from infra.logging import log
from security.vault import decrypt_secret, encrypt_secret
from tools import TOOL_REGISTRY
from tools.mcp_tool import MCP_PREFIX, MCPTool


def _decrypt_env_json(raw: str | None) -> dict:
    """Dekripsi kolom `mcp_servers.env`, dengan fallback ke baris LAMA yang masih
    plaintext JSON (pra-enkripsi, audit 2026-07-28) — lihat security/vault.py.
    Fail-safe: key salah/tak diset/JSON rusak → {} (server tetap dimuat tanpa
    env, bukan crash)."""
    if not raw:
        return {}
    try:
        raw = decrypt_secret(raw)
    except (InvalidToken, ValueError):
        pass  # bukan ciphertext valid (baris lama) atau key belum diset — coba apa adanya
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


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
        env_value = json.dumps({})
        if env:
            # Audit 2026-07-28: env var subprocess MCP bisa berisi API key/token —
            # enkripsi-at-rest (CLAUDE.md §1.2), bukan plaintext JSON. Dict kosong
            # tak perlu dienkripsi (tak ada rahasia buat disembunyikan).
            try:
                env_value = encrypt_secret(json.dumps(env))
            except ValueError as e:
                return {"error": str(e)}
        try:
            await self.db.execute(
                """INSERT INTO mcp_servers (name, transport, command, url, env, enabled)
                   VALUES (?,?,?,?,?,1)""",
                (
                    name,
                    transport,
                    json.dumps(command or []),
                    url.strip(),
                    env_value,
                ),
            )
        except Exception as e:  # noqa: BLE001 — kemungkinan UNIQUE(name)
            return {"error": f"gagal menambah server (nama mungkin sudah ada): {e}"}
        return {"ok": True, "name": name}

    async def list_servers(self) -> list[dict]:
        """Daftar server MCP untuk UI/admin.

        `env` (env var subprocess MCP stdio, bisa berisi API key/token server
        itu) dienkripsi-at-rest di DB (security/vault.py, CLAUDE.md §1.2/§7
        Pengecualian sadar #4) — tapi read-path publik ini TETAP TIDAK PERNAH
        mengembalikan nilai mentah (walau sudah terenkripsi), hanya `has_env`
        (server ini punya env var atau tidak) — defense-in-depth, mencegah
        ciphertext ikut ke UI/API tanpa perlu.
        """
        rows = await self.db.fetchall("SELECT * FROM mcp_servers ORDER BY id DESC")
        for row in rows:
            row["has_env"] = bool(_decrypt_env_json(row.get("env")))
            row.pop("env", None)
        return rows

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
        return MCPServerConfig(
            name=row["name"],
            transport=row["transport"],
            command=command,
            url=row.get("url") or "",
            env=_decrypt_env_json(row.get("env")),
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
