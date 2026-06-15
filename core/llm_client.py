import httpx
import json
from dataclasses import dataclass, field
from typing import AsyncGenerator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from infra.config import AppConfig
from infra.logging import log


@dataclass
class LLMChunk:
    type: str  # text | tool_call | usage | fallback
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


class ProviderUnavailable(Exception):
    pass


class LLMClient:
    """Entry point tunggal untuk semua interaksi LLM. Jangan call LLM langsung dari modul lain."""

    def __init__(self, vault, config: AppConfig):
        self.vault = vault
        self.config = config

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
                    yield LLMChunk(type="fallback")

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
            return True  # anthropic: asumsikan up, retry handle transient
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
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = data.get("message", {})
                    if msg.get("content"):
                        yield LLMChunk(type="text", text=msg["content"])
                    for tc in msg.get("tool_calls", []):
                        yield LLMChunk(
                            type="tool_call",
                            tool_name=tc["function"]["name"],
                            tool_input=tc["function"]["arguments"],
                        )
                    if data.get("done") and data.get("prompt_eval_count"):
                        yield LLMChunk(
                            type="usage",
                            usage={
                                "input_tokens": data.get("prompt_eval_count", 0),
                                "output_tokens": data.get("eval_count", 0),
                            },
                        )

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
                    elif etype == "message_delta":
                        if data.get("usage"):
                            yield LLMChunk(type="usage", usage=data["usage"])
