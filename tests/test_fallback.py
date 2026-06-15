import pytest
from unittest.mock import AsyncMock
from core.llm_client import LLMClient, LLMChunk, ProviderUnavailable
from infra.config import AppConfig


@pytest.fixture
def config():
    return AppConfig(
        fallback_chain=(
            ("ollama", "qwen2.5:7b"),
            ("anthropic", "claude-haiku-4-5-20251001"),
        )
    )


@pytest.fixture
def client(config):
    vault = AsyncMock()
    vault.get.return_value = "test-key"
    return LLMClient(vault=vault, config=config)


async def _text_gen(text="ok"):
    """Helper: async generator yang yield satu LLMChunk."""
    yield LLMChunk(type="text", text=text)


@pytest.mark.asyncio
async def test_fallback_when_ollama_down(client):
    """Audit #5: jika Ollama offline, harus turun ke fallback berikutnya."""

    async def mock_health(provider: str) -> bool:
        return provider != "ollama"  # ollama selalu gagal

    client._health_check = mock_health

    async def mock_stream_one(prov, mdl, messages, tools, max_tokens):
        if prov == "anthropic":
            yield LLMChunk(type="text", text="fallback ok")

    client._stream_one = mock_stream_one

    chunks = []
    async for chunk in client.stream_with_fallback("ollama", "qwen2.5:7b", []):
        chunks.append(chunk)

    # Satu chunk type="fallback" (signal) + satu chunk type="text" dari anthropic
    fallback_signals = [c for c in chunks if c.type == "fallback"]
    text_chunks = [c for c in chunks if c.type == "text"]
    assert len(fallback_signals) == 1, "harus ada satu fallback signal"
    assert len(text_chunks) == 1
    assert text_chunks[0].text == "fallback ok"


@pytest.mark.asyncio
async def test_fallback_signal_not_emitted_for_primary(client):
    """Primary berhasil → tidak ada chunk type='fallback'."""

    async def mock_health(provider: str) -> bool:
        return True

    async def mock_stream(prov, mdl, messages, tools, max_tokens):
        yield LLMChunk(type="text", text="ok")

    client._health_check = mock_health
    client._stream_one = mock_stream

    chunks = []
    async for chunk in client.stream_with_fallback("ollama", "qwen2.5:7b", []):
        chunks.append(chunk)

    assert not any(c.type == "fallback" for c in chunks)


@pytest.mark.asyncio
async def test_all_providers_fail_raises(client):
    """Jika semua provider gagal, harus raise ProviderUnavailable."""

    async def always_down(provider: str) -> bool:
        return False

    client._health_check = always_down

    with pytest.raises(ProviderUnavailable):
        async for _ in client.stream_with_fallback("ollama", "qwen2.5:7b", []):
            pass


@pytest.mark.asyncio
async def test_primary_success_no_fallback(client):
    """Jika provider utama berhasil, fallback tidak dipanggil."""
    calls: list[str] = []

    async def mock_health(provider: str) -> bool:
        return True

    async def mock_stream(prov, mdl, messages, tools, max_tokens):
        calls.append(prov)
        yield LLMChunk(type="text", text="primary ok")

    client._health_check = mock_health
    client._stream_one = mock_stream

    async for _ in client.stream_with_fallback("ollama", "qwen2.5:7b", []):
        pass

    assert calls == ["ollama"]  # hanya primary, fallback tidak dipanggil
