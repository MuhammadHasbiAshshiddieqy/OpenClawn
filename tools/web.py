import httpx

from infra.config import CONFIG
from tools.base import Tool

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
MAX_BODY = CONFIG.tool_max_output


class WebFetchTool(Tool):
    name = "web_fetch"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
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
            "description": "Ambil konten mentah dari satu URL (GET). Untuk mencari di web pakai web_search.",
            "input_schema": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        }


class WebSearchTool(Tool):
    """Cari di web via Tavily API. API key diambil dari Vault, tak pernah ke prompt."""

    name = "web_search"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        query = (input_data.get("query") or "").strip()
        if not query:
            return {"error": "query wajib diisi"}
        try:
            # Vault: kredensial hanya diinjeksi saat outbound, tak pernah masuk context (§1.2).
            api_key = await vault.get("TAVILY_API_KEY")
        except ValueError:
            return {
                "error": (
                    "web_search butuh TAVILY_API_KEY di environment. "
                    "Dapatkan gratis di https://tavily.com lalu set di .env."
                )
            }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": int(input_data.get("max_results", 5)),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            return {"error": f"Pencarian gagal: {e}"}

        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:500],
            }
            for r in data.get("results", [])
        ]
        return {"query": query, "results": results, "answer": data.get("answer", "")}

    def schema(self) -> dict:
        return {
            "name": "web_search",
            "description": (
                "Cari informasi di web (mengembalikan judul, URL, cuplikan). "
                "Pakai ini untuk pertanyaan faktual/terkini, lalu web_fetch untuk membaca URL hasilnya."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "description": "Jumlah hasil (default 5)."},
                },
                "required": ["query"],
            },
        }


class HttpRequestTool(Tool):
    """HTTP request generik ke API eksternal. Destruktif (bisa POST/DELETE) → approval."""

    name = "http_request"
    requires_approval = True

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        url = (input_data.get("url") or "").strip()
        method = (input_data.get("method") or "GET").upper()
        headers = input_data.get("headers") or {}
        body = input_data.get("body")
        if not url:
            return {"error": "url wajib diisi"}
        if not url.startswith(("http://", "https://")):
            return {"error": "url harus diawali http:// atau https://"}
        if method not in ALLOWED_METHODS:
            return {"error": f"method tidak didukung: {method}"}
        if not isinstance(headers, dict):
            return {"error": "headers harus berupa object key-value"}

        # Header yang menyebut kredensial diambil dari Vault, bukan dari prompt model.
        # Konvensi: nilai header berbentuk "vault:NAMA_KEY" akan di-resolve di sini.
        try:
            resolved_headers = {}
            for k, v in headers.items():
                if isinstance(v, str) and v.startswith("vault:"):
                    resolved_headers[k] = await vault.get(v[len("vault:") :])
                else:
                    resolved_headers[k] = v
        except ValueError as e:
            return {"error": f"Kredensial vault tidak ditemukan: {e}"}

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                kwargs: dict = {"headers": resolved_headers}
                if body is not None:
                    if isinstance(body, (dict, list)):
                        kwargs["json"] = body
                    else:
                        kwargs["content"] = str(body)
                resp = await client.request(method, url, **kwargs)
            text = resp.text[:MAX_BODY]
            return {
                "status": resp.status_code,
                "body": text,
                "truncated": len(resp.text) > MAX_BODY,
            }
        except httpx.HTTPError as e:
            return {"error": str(e)}

    def schema(self) -> dict:
        return {
            "name": "http_request",
            "description": (
                "Panggil HTTP API eksternal (GET/POST/PUT/PATCH/DELETE) dengan header & body. "
                "Untuk kredensial, set nilai header ke 'vault:NAMA_KEY' agar diambil aman dari Vault "
                "(jangan tulis API key langsung). SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {
                        "type": "string",
                        "description": "GET (default)/POST/PUT/PATCH/DELETE",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Header opsional. Nilai 'vault:KEY' di-resolve dari Vault.",
                    },
                    "body": {"description": "Body request (object→JSON, atau string)."},
                },
                "required": ["url"],
            },
        }
