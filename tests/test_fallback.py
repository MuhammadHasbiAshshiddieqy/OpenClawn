from unittest.mock import AsyncMock

import httpx
import pytest

from core.llm_client import LLMChunk, LLMClient, ProviderUnavailable
from infra.config import AppConfig


@pytest.fixture
def config():
    return AppConfig(
        fallback_chain=(
            ("ollama", "gemma4:e4b"),
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
    async for chunk in client.stream_with_fallback("ollama", "gemma4:e4b", []):
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
    async for chunk in client.stream_with_fallback("ollama", "gemma4:e4b", []):
        chunks.append(chunk)

    assert not any(c.type == "fallback" for c in chunks)


@pytest.mark.asyncio
async def test_all_providers_fail_raises(client):
    """Jika semua provider gagal, harus raise ProviderUnavailable."""

    async def always_down(provider: str) -> bool:
        return False

    client._health_check = always_down

    with pytest.raises(ProviderUnavailable):
        async for _ in client.stream_with_fallback("ollama", "gemma4:e4b", []):
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

    async for _ in client.stream_with_fallback("ollama", "gemma4:e4b", []):
        pass

    assert calls == ["ollama"]  # hanya primary, fallback tidak dipanggil


@pytest.mark.asyncio
async def test_stream_one_retries_transient_error_before_first_chunk(client, monkeypatch):
    """Audit produksi 2026-07-27: retry di _stream_one HARUS benar-benar terjadi
    untuk httpx.HTTPError yang muncul sebelum chunk pertama (regresi dari
    @retry tenacity yang tak pernah retry generator sungguhan)."""
    monkeypatch.setattr("core.llm_client.asyncio.sleep", AsyncMock())
    attempts: list[int] = []

    async def flaky(provider, model, messages, tools, max_tokens):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("transient")
        yield LLMChunk(type="text", text="recovered")

    client._stream_one_attempt = flaky

    chunks = [c async for c in client._stream_one("ollama", "gemma4:e4b", [], None, 100)]

    assert len(attempts) == 3, "harus retry sampai berhasil, bukan langsung menyerah"
    assert chunks == [LLMChunk(type="text", text="recovered")]


@pytest.mark.asyncio
async def test_stream_one_no_retry_after_first_chunk_sent(client, monkeypatch):
    """Kegagalan SETELAH chunk pertama terkirim tidak boleh di-retry (akan
    menduplikasi output yang sudah terlanjur dikirim ke user) — harus propagate
    langsung supaya stream_with_fallback yang menangani (pindah provider)."""
    monkeypatch.setattr("core.llm_client.asyncio.sleep", AsyncMock())
    attempts: list[int] = []

    async def fails_midstream(provider, model, messages, tools, max_tokens):
        attempts.append(1)
        yield LLMChunk(type="text", text="partial")
        raise httpx.ReadTimeout("dropped mid-stream")

    client._stream_one_attempt = fails_midstream

    with pytest.raises(httpx.ReadTimeout):
        async for _ in client._stream_one("ollama", "gemma4:e4b", [], None, 100):
            pass

    assert len(attempts) == 1, "tidak boleh retry setelah sebagian stream terkirim"


# ── Audit 2026-09-25 (#6): API key hilang = provider tak tersedia ───────────


@pytest.mark.asyncio
async def test_missing_api_key_falls_back_instead_of_crashing(config):
    """Reproduksi temuan: Vault.get me-raise ValueError yang lolos dari fallback
    chain — Ollama tak pernah dicoba, turn crash."""
    vault = AsyncMock()
    vault.get.side_effect = ValueError("Credential 'ANTHROPIC_API_KEY' tidak ditemukan")
    client = LLMClient(vault=vault, config=config)

    async def mock_health(provider: str) -> bool:
        return True

    client._health_check = mock_health

    async def fake_ollama(model, messages, tools, max_tokens):
        yield LLMChunk(type="text", text="lokal ok")

    client._ollama = fake_ollama

    chunks = [
        c
        async for c in client.stream_with_fallback(
            "anthropic", "claude-haiku-4-5-20251001", [{"role": "user", "content": "x"}]
        )
    ]
    assert any(c.type == "text" and c.text == "lokal ok" for c in chunks)
    assert any(c.type == "fallback" for c in chunks)


@pytest.mark.asyncio
async def test_unknown_provider_raises_instead_of_silent_empty(client):
    with pytest.raises(ProviderUnavailable):
        async for _ in client._stream_one_attempt("typo-provider", "m", [], None, 10):
            pass


@pytest.mark.asyncio
async def test_resolve_previous_refine_failure_does_not_crash_turn():
    """Refine (LLM evaluator) gagal → baris pending TETAP ditandai resolved,
    tak dicoba ulang (dan gagal) di setiap turn berikutnya."""
    from infra.database import DatabaseManager
    from memory.skill_decay import SkillDecayManager
    from memory.skill_feedback import SkillFeedback

    cfg = AppConfig(db_path=":memory:")
    db = DatabaseManager(cfg)
    conn = await db.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    try:
        cur = await db.execute(
            "INSERT INTO skills (role, skill_name, skill_content, status) VALUES ('pm','s','x','active')"
        )
        crystallizer = AsyncMock()
        crystallizer.refine_on_correction.side_effect = ProviderUnavailable("semua gagal")
        fb = SkillFeedback("pm", db, SkillDecayManager("pm", db, cfg), crystallizer, cfg)
        await fb.record_usage("s1", [cur.lastrowid])
        await fb.resolve_previous("s1", corrected=True, correction_trace="salah")
        row = await db.fetchone("SELECT resolved FROM skill_usage_pending")
        assert row["resolved"] == 1
    finally:
        await db.close()
