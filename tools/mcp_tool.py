"""Bungkus tool MCP eksternal sebagai `Tool` OpenCLAWN.

Kunci integrasi aman: tool MCP TIDAK mendapat jalur istimewa. Ia menjadi subclass
`Tool` biasa di `TOOL_REGISTRY` yang sama, sehingga otomatis melewati SEMUA pagar
yang ada: izin per-role (`_tool_allowed`), validasi schema, telemetri, timeout, dan
— yang terpenting — `requires_approval`.

KEAMANAN (§1): tool dari server eksternal = TAK TERKENDALI → `requires_approval=True`
SELALU (HITL). Di mode autopilot otomatis jadi proposal (tak pernah eksekusi senyap).
Nama di-prefix `mcp__<server>__<tool>` agar tak bentrok dengan tool bawaan & jelas
asalnya bagi user yang meng-approve.
"""

from core.mcp_client import MCPClient, MCPToolSpec
from tools.base import Tool

# Prefix nama agar tool MCP selalu dapat dibedakan dari 26 tool bawaan.
MCP_PREFIX = "mcp__"


def mcp_tool_name(server: str, tool: str) -> str:
    """Nama terdaftar untuk tool MCP: `mcp__<server>__<tool>`."""
    return f"{MCP_PREFIX}{server}__{tool}"


class MCPTool(Tool):
    """Adapter: satu tool MCP → antarmuka `Tool`. SELALU butuh approval (§1)."""

    # Server eksternal tak terkendali → wajib HITL. Tidak bisa di-override.
    requires_approval = True

    def __init__(self, spec: MCPToolSpec, client: MCPClient):
        self._spec = spec
        self._client = client
        self.name = mcp_tool_name(spec.server, spec.name)

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        # Buang field internal (mis. _session_id) yang mungkin disuntik AgentLoop —
        # tool MCP hanya menerima argumen yang dideklarasikan server.
        args = {k: v for k, v in input_data.items() if not k.startswith("_")}
        return await self._client.call_tool(self._spec.name, args)

    def schema(self) -> dict:
        # Schema dari server MCP, dipakai LLM untuk memanggil + divalidasi AgentLoop.
        desc = self._spec.description or f"Tool MCP dari server '{self._spec.server}'."
        return {
            "name": self.name,
            "description": f"[MCP:{self._spec.server}] {desc} (butuh persetujuan)",
            "input_schema": self._spec.input_schema or {"type": "object", "properties": {}},
        }
