import httpx
from tools.base import Tool


class WebFetchTool(Tool):
    name = "web_fetch"
    requires_approval = False

    async def execute(self, input_data: dict, vault) -> dict:
        url = input_data.get("url", "")
        if not url:
            return {"error": "url wajib diisi"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                return {"status": resp.status_code, "content": resp.text[:5000]}
        except httpx.HTTPError as e:
            return {"error": str(e)}

    def schema(self) -> dict:
        return {
            "name": "web_fetch",
            "description": "Ambil konten dari URL",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        }
