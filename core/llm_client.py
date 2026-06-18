import httpx
import json
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from infra.config import AppConfig
from infra.logging import log


# ── Plain-text tool call parsers ───────────────────────────────────────────────
# Banyak model GGUF lokal mengeluarkan tool call sebagai token teks biasa,
# bukan sebagai message.tool_calls terstruktur di JSON response Ollama.
# Regex di bawah menangkap tiap format dari keluarga model yang berbeda.

# Gemma 4: <|tool_call>call:NAME{args}<tool_call|>
_RE_GEMMA_TC = re.compile(
    r"<\|tool_call>\s*call:\s*(\w+)\s*(\{.*?\})\s*<tool_call\|>", re.DOTALL
)

# Qwen 2.5 / 3: <tool_call>\n{"name": "NAME", "arguments": {...}}\n</tool_call>
_RE_QWEN_TC = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)

# Llama 3.1 / 3.2: <|python_tag|>{"name": "NAME", "parameters": {...}}
# Greedy sampai akhir string — tool call selalu di ujung respons Llama3.
_RE_LLAMA3_TC = re.compile(
    r"<\|python_tag\|>\s*(\{.*\})\s*$", re.DOTALL
)

# Mistral / Mixtral: [TOOL_CALLS] [{"name": "NAME", "arguments": {...}}, ...]
_RE_MISTRAL_TC = re.compile(
    r"\[TOOL_CALLS\]\s*(\[.*?\])", re.DOTALL
)

# DeepSeek: <｜tool▁calls▁begin｜>...<｜tool▁call▁begin｜>{...}<｜tool▁call▁end｜>
_RE_DEEPSEEK_TC = re.compile(
    r"<｜tool▁call▁begin｜>\s*(\{.*?\}|.+?)\s*<｜tool▁call▁end｜>", re.DOTALL
)

# Functionary v3: <|from|>assistant\n<|recipient|>NAME\n<|content|>{args}\n<|stop|>
_RE_FUNCTIONARY_TC = re.compile(
    r"<\|from\|>assistant\n<\|recipient\|>(\w+)\n<\|content\|>(.*?)<\|stop\|>", re.DOTALL
)

# Generic <tool_code>NAME</tool_code> atau <tool_code>NAME\n{args}</tool_code>
_RE_TOOL_CODE_TC = re.compile(
    r"<tool_code>\s*(\w+)\s*(\{.*?\})?\s*</tool_code>", re.DOTALL
)

# Pola untuk mendeteksi SEMUA prefix tool call (untuk strip dari output teks)
_RE_TOOL_STRIP = re.compile(
    r"(<\|tool_call>.*?(?:<tool_call\|>|$))"          # Gemma
    r"|(<tool_call>.*?(?:</tool_call>|$))"             # Qwen
    r"|(<\|python_tag\|>.*?(?:\n|$))"                  # Llama3
    r"|(\[TOOL_CALLS\].*?(?:\]|$))"                    # Mistral
    r"|(<｜tool▁call▁begin｜>.*?(?:<｜tool▁call▁end｜>|$))"  # DeepSeek
    r"|(<\|from\|>assistant.*?(?:<\|stop\|>|$))"       # Functionary
    r"|(<tool_code>.*?(?:</tool_code>|$))",            # Generic
    re.DOTALL,
)

# Daftar parser: (regex, parser_name, has_named_groups)
_PLAINTEXT_PARSERS: list[tuple[re.Pattern, str]] = [
    (_RE_GEMMA_TC, "gemma"),
    (_RE_QWEN_TC, "qwen"),
    (_RE_LLAMA3_TC, "llama3"),
    (_RE_MISTRAL_TC, "mistral"),
    (_RE_DEEPSEEK_TC, "deepseek"),
    (_RE_FUNCTIONARY_TC, "functionary"),
    (_RE_TOOL_CODE_TC, "tool_code"),
]


@dataclass
class LLMChunk:
    type: str  # text | thinking | tool_call | usage | fallback
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    fallback_used: bool = False
    fallback_model: str = ""


class ProviderUnavailable(Exception):
    pass


# Tag reasoning yang dipakai model lokal (deepseek-r1, qwen, dsb). Beberapa model
# memakai variasi; kita kenali keduanya. Kasus paling umum: <think>...</think>.
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class ThinkTagSplitter:
    """Pisahkan `<think>...</think>` dari teks jawaban secara STREAMING.

    Model GGUF lokal menaruh reasoning inline sebagai `<think>...</think>` di
    dalam content. Karena di-stream token demi token, tag bisa terpotong di
    tengah (mis. `<thi` lalu `nk>`). Splitter ini menahan ekor yang berpotensi
    bagian dari tag sampai pasti, lalu mengklasifikasikan tiap potongan sebagai
    ("thinking", teks) atau ("text", teks).

    Pemakaian: panggil `feed(chunk)` untuk tiap potongan stream → list of
    (kind, text); panggil `flush()` di akhir untuk sisa buffer.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        out: list[tuple[str, str]] = []
        while True:
            marker = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = self._buf.find(marker)
            if idx == -1:
                # Tidak ada marker utuh. Emit bagian yang PASTI bukan awal marker,
                # tahan ekor yang mungkin prefix marker (mis. "<thi").
                safe = self._emit_safe_prefix(marker)
                if safe:
                    out.append((self._kind(), safe))
                break
            # Marker ditemukan: emit teks sebelum marker, lalu toggle mode.
            before = self._buf[:idx]
            if before:
                out.append((self._kind(), before))
            self._buf = self._buf[idx + len(marker) :]
            self._in_think = not self._in_think
        return [(k, t) for k, t in out if t]

    def flush(self) -> list[tuple[str, str]]:
        """Emit sisa buffer di akhir stream (tag tak tertutup → anggap apa adanya)."""
        rest = self._buf
        self._buf = ""
        return [(self._kind(), rest)] if rest else []

    def _kind(self) -> str:
        return "thinking" if self._in_think else "text"

    def _emit_safe_prefix(self, marker: str) -> str:
        """Kembalikan bagian buffer yang aman dikirim; tahan ekor yang bisa jadi
        awal `marker` (agar tag terpotong tidak bocor sebagai teks)."""
        keep = 0
        for n in range(1, min(len(marker), len(self._buf)) + 1):
            if self._buf[-n:] == marker[:n]:
                keep = n
        if keep:
            safe, self._buf = self._buf[:-keep], self._buf[-keep:]
            return safe
        safe, self._buf = self._buf, ""
        return safe


class LLMClient:
    """Entry point tunggal untuk semua interaksi LLM. Jangan call LLM langsung dari modul lain."""

    def __init__(self, vault, config: AppConfig):
        self.vault = vault
        self.config = config

    @staticmethod
    def parse_plaintext_tool_calls(text: str) -> tuple[str, list[dict]]:
        """Parse tool call dari plain text token yang disisipkan model lokal.

        Banyak model GGUF (Gemma, Qwen, Llama, Mistral, DeepSeek) mengeluarkan
        tool call sebagai token teks di stream content, bukan sebagai field
        terstruktur di JSON response.

        Return: (cleaned_text, list_of_tool_calls)
          - cleaned_text: teks asli tanpa token tool call
          - tool_calls: [{"name": "...", "input": {...}}, ...]
        """
        cleaned = text
        tool_calls: list[dict] = []

        for pattern, family in _PLAINTEXT_PARSERS:
            for match in pattern.finditer(text):
                try:
                    name, args = LLMClient._extract_tool_call(match, family)
                    if name:
                        tool_calls.append({"name": name, "input": args})
                except (json.JSONDecodeError, TypeError, AttributeError):
                    log.debug("plaintext_tool_parse_failed", family=family)
                    continue

        # Strip SEMUA tool call token dari teks output
        if tool_calls:
            cleaned = _RE_TOOL_STRIP.sub("", text).strip()

        return cleaned, tool_calls

    @staticmethod
    def _extract_tool_call(match: re.Match, family: str) -> tuple[str | None, dict]:
        """Ekstrak nama tool + arguments dari regex match berdasarkan family."""
        args: dict = {}

        if family == "gemma":
            name = match.group(1)
            raw_args = match.group(2)
            args = json.loads(raw_args)
        elif family == "qwen":
            data = json.loads(match.group(1))
            name = data.get("name", "")
            args = data.get("arguments", {})
        elif family == "llama3":
            data = json.loads(match.group(1))
            name = data.get("name", "")
            args = data.get("parameters", data.get("arguments", {}))
        elif family == "mistral":
            tools = json.loads(match.group(1))
            if tools and isinstance(tools, list):
                first = tools[0]
                name = first.get("name", "")
                args = first.get("arguments", {})
            else:
                name = None
        elif family == "deepseek":
            raw = match.group(1).strip()
            try:
                data = json.loads(raw)
                name = data.get("name", "")
                args = data.get("arguments", {})
            except json.JSONDecodeError:
                # DeepSeek kadang mengeluarkan nama tool tanpa JSON
                name = raw
        elif family == "functionary":
            name = match.group(1)
            raw_args = match.group(2).strip()
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"raw": raw_args}
        elif family == "tool_code":
            name = match.group(1)
            raw_args = match.group(2)
            if raw_args:
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {"raw": raw_args.strip()}
        else:
            name = None

        return name, args

    async def stream_with_fallback(
        self,
        provider: str,
        model: str,
        messages: list,
        tools: list | None = None,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[LLMChunk, None]:
        """
        Coba provider utama. Jika gagal (offline/error), turun ke fallback chain.
        Audit #5: graceful degradation.
        """
        chain = [(provider, model)] + [
            fc for fc in self.config.fallback_chain if fc != (provider, model)
        ]

        last_error: Exception | None = None
        for idx, (prov, mdl) in enumerate(chain):
            try:
                if not await self._health_check(prov):
                    raise ProviderUnavailable(f"{prov} health check gagal")

                if idx > 0:
                    log.warning("llm_fallback", from_model=model, to_model=mdl, attempt=idx)
                    # Signal ke consumer bahwa fallback aktif, untuk audit logging
                    yield LLMChunk(type="fallback", fallback_used=True, fallback_model=mdl)

                async for chunk in self._stream_one(prov, mdl, messages, tools, max_tokens):
                    yield chunk
                return  # sukses

            except (httpx.HTTPError, ProviderUnavailable) as e:
                last_error = e
                log.error("llm_provider_failed", provider=prov, model=mdl, error=str(e))
                continue

        raise ProviderUnavailable(f"Semua provider gagal. Terakhir: {last_error}")

    async def _health_check(self, provider: str) -> bool:
        try:
            if provider == "ollama":
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(f"{self.config.ollama_base}/api/tags")
                    return r.status_code == 200
            return True  # anthropic/gemini: asumsikan up, retry handle transient
        except httpx.HTTPError:
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _stream_one(
        self,
        provider: str,
        model: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> AsyncGenerator[LLMChunk, None]:
        """Retry transient errors dengan exponential backoff."""
        if provider == "ollama":
            async for c in self._ollama(model, messages, tools, max_tokens):
                yield c
        elif provider == "anthropic":
            async for c in self._claude(model, messages, tools, max_tokens):
                yield c
        elif provider == "gemini":
            async for c in self._gemini(model, messages, max_tokens):
                yield c

    async def _ollama(
        self, model: str, messages: list, tools: list | None, max_tokens: int
    ) -> AsyncGenerator[LLMChunk, None]:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{self.config.ollama_base}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                # Streaming teks + akumulasi untuk deteksi plaintext tool call.
                # Teks DIKIRIM real-time agar browser tidak timeout; buffer
                # disimpan untuk post-scan tool call di akhir stream.
                text_buf: list[str] = []
                native_tool_calls: list[dict] = []
                usage_data: dict = {}
                # Splitter memisahkan <think>…</think> inline dari jawaban. Tool call
                # tidak pernah di dalam <think>, jadi hanya bagian "text" yang masuk
                # text_buf untuk deteksi plaintext tool call.
                splitter = ThinkTagSplitter()

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = data.get("message", {})
                    # Field thinking terpisah (Ollama API baru / model reasoning).
                    if msg.get("thinking"):
                        yield LLMChunk(type="thinking", text=msg["thinking"])
                    if msg.get("content"):
                        for kind, piece in splitter.feed(msg["content"]):
                            if kind == "text":
                                text_buf.append(piece)
                            yield LLMChunk(type=kind, text=piece)
                    for tc in msg.get("tool_calls", []):
                        native_tool_calls.append({
                            "name": tc["function"]["name"],
                            "input": tc["function"]["arguments"],
                        })
                    if data.get("done") and data.get("prompt_eval_count"):
                        usage_data = {
                            "input_tokens": data.get("prompt_eval_count", 0),
                            "output_tokens": data.get("eval_count", 0),
                        }

                # Flush sisa buffer splitter (mis. teks tanpa tag penutup di akhir).
                for kind, piece in splitter.flush():
                    if kind == "text":
                        text_buf.append(piece)
                    yield LLMChunk(type=kind, text=piece)

                # Post-processing: deteksi tool call plain-text di akumulasi teks.
                # Tool call dari model GGUF (Gemma, Qwen, dsb.) muncul sebagai
                # token teks di content. Kita scan di akhir stream — teks mentah
                # (termasuk token <|tool_call|>) sudah terlanjur dikirim ke user,
                # tapi tool akan tetap tereksekusi dan hasilnya muncul berikutnya.
                raw_text = "".join(text_buf)
                _, parsed_calls = LLMClient.parse_plaintext_tool_calls(raw_text)

                all_calls = native_tool_calls + parsed_calls
                for tc in all_calls:
                    yield LLMChunk(
                        type="tool_call",
                        tool_name=tc["name"],
                        tool_input=tc.get("input", {}),
                    )

                if usage_data:
                    yield LLMChunk(type="usage", usage=usage_data)

    async def _claude(
        self, model: str, messages: list, tools: list | None, max_tokens: int
    ) -> AsyncGenerator[LLMChunk, None]:
        api_key = await self.vault.get("ANTHROPIC_API_KEY")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]

        # Prompt caching: system prompt stabil → cache_control ephemeral
        # Hemat hingga 90% biaya untuk bagian yang berulang (audit gap)
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": user_msgs,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST",
                f"{self.config.anthropic_base}/v1/messages",
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip())
                    etype = data.get("type", "")
                    if etype == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            yield LLMChunk(
                                type="tool_call", tool_name=block.get("name", ""), tool_input={}
                            )
                    elif etype == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield LLMChunk(type="text", text=delta["text"])
                        elif delta.get("type") == "thinking_delta":
                            # Extended thinking Anthropic → blok reasoning terpisah.
                            yield LLMChunk(type="thinking", text=delta.get("thinking", ""))
                    elif etype == "message_delta":
                        if data.get("usage"):
                            yield LLMChunk(type="usage", usage=data["usage"])

    async def _gemini(
        self, model: str, messages: list, max_tokens: int
    ) -> AsyncGenerator[LLMChunk, None]:
        """Google AI Studio (generativelanguage). Raw httpx, SSE streaming.

        Catatan: Gemini memakai peran 'user'/'model' (bukan 'assistant'/'system')
        dan struktur 'contents'/'parts' — kita konversi dari format internal.
        Tool calling Gemini belum didukung di sini (cukup teks); audit/crystallizer
        yang butuh teks JSON tetap berfungsi.
        """
        api_key = await self.vault.get("GOOGLE_API_KEY")

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
            if m["role"] in ("user", "assistant") and m.get("content")
        ]

        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.config.gemini_base}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        headers = {"content-type": "application/json", "x-goog-api-key": api_key}

        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip())
                    for cand in data.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if part.get("text"):
                                # parts dengan thought=true adalah reasoning Gemini.
                                kind = "thinking" if part.get("thought") else "text"
                                yield LLMChunk(type=kind, text=part["text"])
                    usage = data.get("usageMetadata")
                    if usage:
                        yield LLMChunk(
                            type="usage",
                            usage={
                                "input_tokens": usage.get("promptTokenCount", 0),
                                "output_tokens": usage.get("candidatesTokenCount", 0),
                            },
                        )
