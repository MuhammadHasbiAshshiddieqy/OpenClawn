import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from core.compactor import DYNAMIC_CONTEXT_MARKER
from infra.config import AppConfig
from infra.logging import log

_STREAM_RETRY_ATTEMPTS = 3

# Client httpx shared di seluruh proses (audit produksi 2026-07-27: sebelumnya
# tiap panggilan health-check/stream membuat httpx.AsyncClient baru, membuang
# TCP/TLS handshake + keep-alive tiap turn di bawah traffic konkuren). Timeout
# tetap per-call lewat parameter `timeout=` di tiap method (get/stream), bukan
# default client — tiap provider punya kebutuhan timeout berbeda.
_shared_http_client: httpx.AsyncClient | None = None


def get_shared_http_client() -> httpx.AsyncClient:
    """Client httpx pooled, dibuat lazy dan dipakai ulang lintas request/modul."""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient()
    return _shared_http_client


async def close_shared_http_client() -> None:
    """Tutup client shared saat shutdown — panggil dari lifespan FastAPI."""
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        await _shared_http_client.aclose()
        _shared_http_client = None


# ── Plain-text tool call parsers ───────────────────────────────────────────────
# Banyak model GGUF lokal mengeluarkan tool call sebagai token teks biasa,
# bukan sebagai message.tool_calls terstruktur di JSON response Ollama.
# Regex di bawah menangkap tiap format dari keluarga model yang berbeda.

# Gemma 4: <|tool_call>call:NAME{args}<tool_call|>
_RE_GEMMA_TC = re.compile(r"<\|tool_call>\s*call:\s*(\w+)\s*(\{.*?\})\s*<tool_call\|>", re.DOTALL)

# Qwen 2.5 / 3: <tool_call>\n{"name": "NAME", "arguments": {...}}\n</tool_call>
_RE_QWEN_TC = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

# Llama 3.1 / 3.2: <|python_tag|>{"name": "NAME", "parameters": {...}}
# Greedy sampai akhir string — tool call selalu di ujung respons Llama3.
_RE_LLAMA3_TC = re.compile(r"<\|python_tag\|>\s*(\{.*\})\s*$", re.DOTALL)

# Mistral / Mixtral: [TOOL_CALLS] [{"name": "NAME", "arguments": {...}}, ...]
_RE_MISTRAL_TC = re.compile(r"\[TOOL_CALLS\]\s*(\[.*?\])", re.DOTALL)

# DeepSeek: <｜tool▁calls▁begin｜>...<｜tool▁call▁begin｜>{...}<｜tool▁call▁end｜>
_RE_DEEPSEEK_TC = re.compile(
    r"<｜tool▁call▁begin｜>\s*(\{.*?\}|.+?)\s*<｜tool▁call▁end｜>", re.DOTALL
)

# Functionary v3: <|from|>assistant\n<|recipient|>NAME\n<|content|>{args}\n<|stop|>
_RE_FUNCTIONARY_TC = re.compile(
    r"<\|from\|>assistant\n<\|recipient\|>(\w+)\n<\|content\|>(.*?)<\|stop\|>", re.DOTALL
)

# Generic <tool_code>NAME</tool_code> atau <tool_code>NAME\n{args}</tool_code>
_RE_TOOL_CODE_TC = re.compile(r"<tool_code>\s*(\w+)\s*(\{.*?\})?\s*</tool_code>", re.DOTALL)

# Pola untuk mendeteksi SEMUA prefix tool call (untuk strip dari output teks)
_RE_TOOL_STRIP = re.compile(
    r"(<\|tool_call>.*?(?:<tool_call\|>|$))"  # Gemma
    r"|(<tool_call>.*?(?:</tool_call>|$))"  # Qwen
    r"|(<\|python_tag\|>.*?(?:\n|$))"  # Llama3
    r"|(\[TOOL_CALLS\].*?(?:\]|$))"  # Mistral
    r"|(<｜tool▁call▁begin｜>.*?(?:<｜tool▁call▁end｜>|$))"  # DeepSeek
    r"|(<\|from\|>assistant.*?(?:<\|stop\|>|$))"  # Functionary
    r"|(<tool_code>.*?(?:</tool_code>|$))",  # Generic
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
    # ID tool call dari provider (Anthropic `toolu_...`) — dipakai AgentLoop untuk
    # memasangkan hasil tool ke panggilannya. Kosong → AgentLoop membuat sendiri.
    tool_id: str = ""
    # Gemini `thoughtSignature` pada part functionCall — harus dikirim balik apa
    # adanya di giliran berikutnya agar konteks reasoning model tak putus.
    tool_signature: str = ""


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


# ── Adapter format pesan per provider ──────────────────────────────────────────
# Audit 2026-09-25 (#7-#9): AgentLoop menyusun riwayat tool dalam format internal
# bergaya OpenAI/Ollama:
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "function": {"name", "arguments"}}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# dan schema tool bergaya Anthropic ({name, description, input_schema}). Sebelumnya
# format itu dikirim APA ADANYA ke semua provider: Anthropic menolak role "tool"
# (400 → retry → fallback, tool calling Claude tak pernah jalan), Gemini membuang
# pesan tool (model tak pernah melihat hasil tool → memanggil ulang sampai loop
# detector), dan Ollama menerima schema tanpa `type/function/parameters` (model
# lokal praktis tak melihat definisi tool). Fungsi di bawah menerjemahkan
# eksplisit per provider — satu tempat, bisa dites tanpa jaringan.


def split_system(system: str) -> tuple[str, str]:
    """(bagian stabil, konteks dinamis) — lihat core/compactor.py::DYNAMIC_CONTEXT_MARKER."""
    stable, _, dynamic = system.partition(DYNAMIC_CONTEXT_MARKER)
    return stable, dynamic


def _plain_system(system: str) -> str:
    """System prompt tanpa penanda internal, untuk provider tanpa cache per-blok."""
    stable, dynamic = split_system(system)
    return f"{stable}\n\n{dynamic}" if dynamic else stable


def _as_blocks(content) -> list:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content}] if content else []


def to_anthropic_messages(messages: list) -> list:
    """Format internal → Messages API Anthropic (tool_use / tool_result blocks).

    Giliran ber-role sama yang berurutan digabung (Anthropic mensyaratkan
    user/assistant bergantian), konten kosong dibuang (ditolak API)."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant" and m.get("tool_calls"):
            blocks = _as_blocks(m.get("content") or "")
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name", ""),
                        "input": fn.get("arguments") or {},
                    }
                )
            entry = {"role": "assistant", "content": blocks}
        elif role == "tool":
            entry = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id") or "",
                        "content": m.get("content") or "",
                    }
                ],
            }
        else:
            content = m.get("content")
            if not content:
                continue
            entry = {"role": "assistant" if role == "assistant" else "user", "content": content}
        if out and out[-1]["role"] == entry["role"]:
            out[-1]["content"] = _as_blocks(out[-1]["content"]) + _as_blocks(entry["content"])
        else:
            out.append(entry)
    # Audit 2026-09-26: truncation/compaction bisa menyisakan giliran assistant di
    # depan — Messages API mensyaratkan giliran pertama "user" (400 bila tidak).
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "[earlier conversation]"})
    return out


def to_gemini_contents(messages: list) -> list:
    """Format internal → `contents` Gemini (functionCall / functionResponse parts)."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant" and m.get("tool_calls"):
            parts: list[dict] = [{"text": m["content"]}] if m.get("content") else []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                part: dict = {
                    "functionCall": {"name": fn.get("name", ""), "args": fn.get("arguments") or {}}
                }
                if tc.get("thought_signature"):
                    part["thoughtSignature"] = tc["thought_signature"]
                parts.append(part)
            entry = {"role": "model", "parts": parts}
        elif role == "tool":
            entry = {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": m.get("name", ""),
                            "response": {"content": m.get("content") or ""},
                        }
                    }
                ],
            }
        else:
            if not m.get("content"):
                continue
            entry = {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
        if out and out[-1]["role"] == entry["role"]:
            out[-1]["parts"] = out[-1]["parts"] + entry["parts"]
        else:
            out.append(entry)
    return out


def to_ollama_tools(tools: list) -> list:
    """Schema internal (Anthropic-style) → format tools Ollama /api/chat."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def to_ollama_messages(messages: list) -> list:
    """Format internal → pesan Ollama (`tool_name` di pesan tool, tanpa field asing)."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": tc.get("function", {}).get("arguments") or {},
                            }
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            out.append(
                {"role": "tool", "content": m.get("content") or "", "tool_name": m.get("name", "")}
            )
        elif role == "system":
            out.append({"role": "system", "content": _plain_system(m.get("content") or "")})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _is_transient(err: httpx.HTTPError) -> bool:
    """Audit 2026-09-25: hanya error SEMENTARA yang layak di-retry (CLAUDE.md §3).
    4xx seperti 400 (payload salah) / 401 (key salah) sebelumnya di-retry 3×
    dengan backoff — membuang waktu dan tak pernah berhasil."""
    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        return code in (408, 409, 425, 429) or code >= 500
    return True


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

            except (httpx.HTTPError, ProviderUnavailable, json.JSONDecodeError) as e:
                # JSONDecodeError: baris stream korup dari provider — sama perlakuannya
                # dengan error transport (coba provider berikutnya), bukan crash turn.
                last_error = e
                log.error("llm_provider_failed", provider=prov, model=mdl, error=str(e))
                continue

        raise ProviderUnavailable(f"Semua provider gagal. Terakhir: {last_error}")

    async def _health_check(self, provider: str) -> bool:
        try:
            if provider == "ollama":
                c = get_shared_http_client()
                r = await c.get(f"{self.config.ollama_base}/api/tags", timeout=3)
                return r.status_code == 200
            return True  # anthropic/gemini: asumsikan up, retry handle transient
        except httpx.HTTPError:
            return False

    async def _stream_one(
        self,
        provider: str,
        model: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> AsyncGenerator[LLMChunk, None]:
        """Retry transient httpx errors dengan exponential backoff.

        Audit produksi 2026-07-27: sebelumnya ini di-decorate `@retry` tenacity
        langsung — TIDAK PERNAH benar-benar retry, karena tenacity membungkus
        *pembuatan* async generator (selalu sukses, lazy), bukan exception yang
        muncul saat *iterasi* `async for` di pemanggil. httpx.HTTPError transien
        di tengah stream langsung lolos ke `stream_with_fallback` dan memicu
        fallback provider, bukan retry di provider yang sama seperti CLAUDE.md §3.

        Retry manual di sini HANYA berlaku sebelum chunk pertama terkirim ke
        caller — begitu satu chunk sudah di-yield (kemungkinan sudah diteruskan
        ke browser via SSE), mengulang dari awal akan menduplikasi/mengacak output
        yang sudah terlihat user, jadi kegagalan setelah itu langsung propagate
        (ditangani via fallback chain, bukan retry di tempat).
        """
        last_error: httpx.HTTPError | None = None
        for attempt_num in range(_STREAM_RETRY_ATTEMPTS):
            started = False
            try:
                async for chunk in self._stream_one_attempt(
                    provider, model, messages, tools, max_tokens
                ):
                    started = True
                    yield chunk
                return
            except httpx.HTTPError as e:
                last_error = e
                if started or attempt_num == _STREAM_RETRY_ATTEMPTS - 1 or not _is_transient(e):
                    raise
                log.warning(
                    "llm_stream_retry",
                    provider=provider,
                    model=model,
                    attempt=attempt_num + 1,
                    error=str(e),
                )
                await asyncio.sleep(min(2**attempt_num, 10))
        if last_error:  # pragma: no cover — unreachable, loop selalu return/raise
            raise last_error

    async def _stream_one_attempt(
        self,
        provider: str,
        model: str,
        messages: list,
        tools: list | None,
        max_tokens: int,
    ) -> AsyncGenerator[LLMChunk, None]:
        if provider == "ollama":
            async for c in self._ollama(model, messages, tools, max_tokens):
                yield c
        elif provider == "anthropic":
            async for c in self._claude(model, messages, tools, max_tokens):
                yield c
        elif provider == "gemini":
            async for c in self._gemini(model, messages, tools, max_tokens):
                yield c
        else:
            # Sebelumnya provider tak dikenal (typo di /router) diam-diam
            # menghasilkan jawaban KOSONG tanpa error apa pun.
            raise ProviderUnavailable(f"provider tidak dikenal: {provider}")

    async def _api_key(self, name: str, provider: str) -> str:
        """Ambil API key dari Vault; key tak diset → ProviderUnavailable.

        Audit 2026-09-25 (#6): Vault.get me-raise ValueError, yang TIDAK
        ditangkap stream_with_fallback — satu provider tanpa key (mis.
        ANTHROPIC_API_KEY kosong, padahal EVALUATOR_FOR memetakan beberapa
        model ke Claude) menjatuhkan seluruh turn tanpa mencoba fallback
        chain sama sekali. Melanggar CLAUDE.md §1.3."""
        try:
            return await self.vault.get(name)
        except ValueError as e:
            raise ProviderUnavailable(f"{provider}: {e}") from e

    async def _ollama(
        self, model: str, messages: list, tools: list | None, max_tokens: int
    ) -> AsyncGenerator[LLMChunk, None]:
        payload: dict = {
            "model": model,
            "messages": to_ollama_messages(messages),
            "stream": True,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = to_ollama_tools(tools)
        client = get_shared_http_client()
        async with client.stream(
            "POST", f"{self.config.ollama_base}/api/chat", json=payload, timeout=120
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
                    native_tool_calls.append(
                        {
                            "name": tc["function"]["name"],
                            "input": tc["function"]["arguments"],
                        }
                    )
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
        api_key = await self._api_key("ANTHROPIC_API_KEY", "anthropic")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system = next((m["content"] for m in messages if m["role"] == "system"), "")

        # Prompt caching: HANYA bagian stabil (soul) yang diberi cache_control —
        # audit 2026-09-26: sebelumnya memori dinamis ikut di blok yang sama, jadi
        # cache berubah tiap turn dan tak pernah kena.
        stable, dynamic = split_system(system)
        system_blocks: list[dict] = [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
        ]
        if dynamic:
            system_blocks.append({"type": "text", "text": dynamic})

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": to_anthropic_messages(messages),
            "stream": True,
        }
        if system:
            payload["system"] = system_blocks
        if tools:
            payload["tools"] = tools

        client = get_shared_http_client()
        async with client.stream(
            "POST",
            f"{self.config.anthropic_base}/v1/messages",
            headers=headers,
            json=payload,
            timeout=180,
        ) as resp:
            resp.raise_for_status()
            # Audit 2026-09-25 (#7): input tool_use datang bertahap lewat
            # `input_json_delta` dan baru lengkap di `content_block_stop` —
            # sebelumnya tool_call di-yield di `content_block_start` dengan input
            # {} (SETIAP tool dari Claude dijalankan tanpa argumen). input_tokens
            # ada di `message_start`, bukan `message_delta` (sebelumnya hilang →
            # biaya Claude tercatat hanya output).
            tool_blocks: dict[int, dict] = {}
            input_tokens = 0
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                etype = data.get("type", "")
                if etype == "message_start":
                    usage = data.get("message", {}).get("usage", {}) or {}
                    input_tokens = usage.get("input_tokens", 0) or 0
                elif etype == "content_block_start":
                    block = data.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_blocks[data.get("index", 0)] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                elif etype == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield LLMChunk(type="text", text=delta["text"])
                    elif delta.get("type") == "thinking_delta":
                        # Extended thinking Anthropic → blok reasoning terpisah.
                        yield LLMChunk(type="thinking", text=delta.get("thinking", ""))
                    elif delta.get("type") == "input_json_delta":
                        blk = tool_blocks.get(data.get("index", 0))
                        if blk is not None:
                            blk["json"] += delta.get("partial_json", "")
                elif etype == "content_block_stop":
                    blk = tool_blocks.pop(data.get("index", 0), None)
                    if blk is not None:
                        try:
                            tool_input = json.loads(blk["json"]) if blk["json"].strip() else {}
                        except json.JSONDecodeError:
                            log.warning("anthropic_tool_input_parse_failed", tool=blk["name"])
                            tool_input = {}
                        yield LLMChunk(
                            type="tool_call",
                            tool_name=blk["name"],
                            tool_input=tool_input if isinstance(tool_input, dict) else {},
                            tool_id=blk["id"],
                        )
                elif etype == "message_delta":
                    usage = data.get("usage") or {}
                    if usage:
                        yield LLMChunk(
                            type="usage",
                            usage={
                                "input_tokens": input_tokens,
                                "output_tokens": usage.get("output_tokens", 0) or 0,
                            },
                        )
                elif etype == "error":
                    # Error di TENGAH stream (mis. overloaded_error) sebelumnya
                    # diabaikan diam-diam → jawaban terpotong/kosong tanpa jejak.
                    err = data.get("error", {}) or {}
                    raise ProviderUnavailable(
                        f"anthropic stream error: {err.get('type', '?')}: {err.get('message', '')}"
                    )

    async def _gemini(
        self, model: str, messages: list, tools: list | None, max_tokens: int
    ) -> AsyncGenerator[LLMChunk, None]:
        """Google AI Studio (generativelanguage). Raw httpx, SSE streaming.

        Catatan: Gemini memakai peran 'user'/'model' (bukan 'assistant'/'system')
        dan struktur 'contents'/'parts' — kita konversi dari format internal.

        Tool calling (§ bug: agent mengklaim menulis PDF tapi tidak pernah
        memanggil tool — router mengalihkan turn bertool ke Gemini, tapi `tools`
        sebelumnya TIDAK PERNAH diteruskan ke sini, jadi model tidak tahu tool
        apa pun ada dan berhalusinasi sudah memanggilnya). Schema internal
        (`Tool.schema()`) berbentuk Anthropic-style (`input_schema`); Gemini
        butuh `functionDeclarations` dengan key `parameters` — dikonversi di sini,
        bukan di tools/*.py, agar tools/ tetap provider-agnostic.
        """
        api_key = await self._api_key("GOOGLE_API_KEY", "gemini")

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        # Audit 2026-09-25 (#8): sebelumnya pesan tool & giliran assistant yang
        # hanya berisi tool call DIBUANG — Gemini (tier default COMPLEX/CRITICAL)
        # tak pernah melihat hasil tool, jadi memanggil tool yang sama berulang.
        contents = to_gemini_contents(messages)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": _plain_system(system)}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get(
                                "input_schema", {"type": "object", "properties": {}}
                            ),
                        }
                        for t in tools
                    ]
                }
            ]

        url = f"{self.config.gemini_base}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        headers = {"content-type": "application/json", "x-goog-api-key": api_key}

        client = get_shared_http_client()
        async with client.stream("POST", url, headers=headers, json=payload, timeout=180) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("functionCall"):
                            fc = part["functionCall"]
                            yield LLMChunk(
                                type="tool_call",
                                tool_name=fc.get("name", ""),
                                tool_input=fc.get("args", {}),
                                tool_signature=part.get("thoughtSignature", ""),
                            )
                        elif part.get("text"):
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
