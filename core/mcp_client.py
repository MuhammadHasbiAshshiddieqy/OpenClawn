"""MCP (Model Context Protocol) client — wrapper tipis di atas SDK resmi `mcp`.

Menyambungkan OpenCLAWN ke server MCP eksternal agar agent bisa memakai tool dari
ekosistem MCP (GitHub, filesystem, dll). SDK resmi dipilih (CLAUDE.md §7) karena
cakupan penuh & ditambal upstream — MCP adalah protokol tool terbuka, bukan SDK
vendor-LLM, jadi tak melanggar prinsip transparansi jalur LLM.

KEAMANAN (§1): server MCP = kode pihak ketiga TAK TERKENDALI. Maka:
- Remote (HTTP) WAJIB lewat `_ssrf_guard` SEBELUM konek — server MCP remote tak boleh
  menjangkau localhost/metadata cloud.
- Tool yang dibungkus dari sini SELALU `requires_approval=True` (tools/mcp_tool.py).
- Koneksi per-panggilan (connect → act → disconnect): sederhana, fail-safe, tak ada
  proses MCP yang menggantung lintas-turn. Error apa pun → ditangkap di boundary.

Extractable: bergantung SDK `mcp` + (`_ssrf_guard` dari tools.web untuk remote).
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from infra.logging import log

# Batas ukuran balasan agar tool MCP tak membanjiri context (token-first §1.4).
MAX_RESULT_CHARS = 10_000
# Timeout konek+aksi per server (server MCP menggantung tak boleh membekukan turn).
CONNECT_TIMEOUT_SEC = 30


class MCPError(Exception):
    """Kegagalan MCP. Selalu ditangkap di boundary publik (fail-safe)."""


@dataclass
class MCPServerConfig:
    """Definisi satu server MCP. `transport` menentukan cara konek."""

    name: str
    transport: str  # "stdio" | "http"
    command: list[str] = field(default_factory=list)  # stdio: argv subprocess
    url: str = ""  # http: endpoint streamable-HTTP server MCP
    env: dict[str, str] = field(default_factory=dict)  # env tambahan untuk stdio


@dataclass
class MCPToolSpec:
    """Tool yang ditemukan dari server MCP (hasil list_tools)."""

    server: str
    name: str
    description: str
    input_schema: dict


class MCPClient:
    """Klien satu server MCP via SDK resmi. Koneksi per-panggilan, fail-safe."""

    def __init__(self, config: MCPServerConfig):
        self.config = config

    @asynccontextmanager
    async def _session(self):
        """Buka transport + ClientSession sesuai config, lalu initialize. Tutup otomatis.

        Remote di-guard SSRF SEBELUM konek (§1). Import SDK lokal agar modul lain yang
        tak memakai MCP tidak menanggung biaya impor.
        """
        from mcp import ClientSession

        if self.config.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            if not self.config.command:
                raise MCPError("server stdio butuh 'command'")
            params = StdioServerParameters(
                command=self.config.command[0],
                args=self.config.command[1:],
                env=self.config.env or None,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif self.config.transport == "http":
            from tools.web import _ssrf_guard

            if not self.config.url.startswith(("http://", "https://")):
                raise MCPError("url MCP harus diawali http:// atau https://")
            blocked = _ssrf_guard(self.config.url)
            if blocked:
                raise MCPError(f"server MCP remote ditolak (SSRF guard): {blocked}")
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self.config.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            raise MCPError(f"transport MCP tak dikenal: {self.config.transport}")

    async def list_tools(self) -> list[MCPToolSpec]:
        """Discover tool dari server. Gagal → [] (fail-safe, tak jatuhkan startup)."""
        try:
            async with self._session() as session:
                result = await session.list_tools()
        except Exception as e:  # noqa: BLE001 — server eksternal, jangan meledak
            log.warning("mcp_list_tools_failed", server=self.config.name, error=str(e))
            return []
        specs: list[MCPToolSpec] = []
        for t in result.tools:
            schema = t.inputSchema or {"type": "object", "properties": {}}
            specs.append(
                MCPToolSpec(
                    server=self.config.name,
                    name=t.name,
                    description=t.description or "",
                    input_schema=schema,
                )
            )
        return specs

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Panggil tool MCP. Error apa pun → {"error": ...} (fail-safe boundary)."""
        try:
            async with self._session() as session:
                result = await session.call_tool(tool_name, arguments or {})
        except Exception as e:  # noqa: BLE001 — boundary ke kode tak terkendali
            log.error("mcp_call_failed", server=self.config.name, tool=tool_name, error=str(e))
            return {"error": f"MCP '{self.config.name}/{tool_name}' gagal: {e}"}
        return self._normalize(result)

    @staticmethod
    def _normalize(result) -> dict:
        """Seragamkan CallToolResult → dict ringkas untuk context agent."""
        text = _extract_text(result.content)
        if getattr(result, "isError", False):
            return {"error": text or "tool MCP mengembalikan error"}
        return {"content": text[:MAX_RESULT_CHARS]}


def _extract_text(content) -> str:
    """Ambil teks dari content blocks MCP (TextContent.type == 'text')."""
    parts: list[str] = []
    for block in content or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(parts)
