"""Test untuk fitur pilih model: SettingsStore, provider Gemini, override routing."""

import pytest
from unittest.mock import AsyncMock

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.settings import SettingsStore
from core.llm_client import LLMClient, LLMChunk


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


# ---------- SettingsStore ----------


@pytest.mark.asyncio
async def test_override_none_by_default(db):
    """Tanpa setting apa pun → mode otomatis (None)."""
    store = SettingsStore(db)
    assert await store.get_model_override() is None


@pytest.mark.asyncio
async def test_set_and_get_override(db):
    """Set override → kebaca kembali sebagai (provider, model)."""
    store = SettingsStore(db)
    await store.set_model_override("gemini", "gemini-2.0-flash")
    assert await store.get_model_override() == ("gemini", "gemini-2.0-flash")


@pytest.mark.asyncio
async def test_clear_override_returns_to_auto(db):
    """Set lalu hapus (None) → kembali ke mode otomatis."""
    store = SettingsStore(db)
    await store.set_model_override("anthropic", "claude-sonnet-4-6")
    await store.set_model_override(None, None)
    assert await store.get_model_override() is None


@pytest.mark.asyncio
async def test_partial_override_is_not_active(db):
    """Hanya provider tanpa model → bukan override valid (tetap otomatis)."""
    store = SettingsStore(db)
    await store.set("model_override_provider", "gemini")
    assert await store.get_model_override() is None


@pytest.mark.asyncio
async def test_override_upsert_overwrites(db):
    """Set dua kali → nilai terakhir menang (tidak menumpuk)."""
    store = SettingsStore(db)
    await store.set_model_override("gemini", "gemini-2.0-flash")
    await store.set_model_override("gemini", "gemini-2.5-pro")
    assert await store.get_model_override() == ("gemini", "gemini-2.5-pro")


# ---------- Provider Gemini di LLMClient ----------


@pytest.fixture
def gemini_client():
    vault = AsyncMock()
    vault.get.return_value = "fake-google-key"
    return LLMClient(vault=vault, config=AppConfig())


@pytest.mark.asyncio
async def test_gemini_provider_dispatch(gemini_client):
    """_stream_one harus mengarahkan provider 'gemini' ke _gemini()."""

    captured = {}

    async def fake_gemini(model, messages, tools, max_tokens):
        captured["model"] = model
        yield LLMChunk(type="text", text="halo dari gemini")

    gemini_client._gemini = fake_gemini

    out = []
    async for chunk in gemini_client._stream_one("gemini", "gemini-2.0-flash", [], None, 100):
        out.append(chunk)

    assert captured["model"] == "gemini-2.0-flash"
    assert out[0].text == "halo dari gemini"


@pytest.mark.asyncio
async def test_gemini_health_check_assumes_up(gemini_client):
    """Gemini (seperti anthropic) diasumsikan up — retry handle transient."""
    assert await gemini_client._health_check("gemini") is True


@pytest.mark.asyncio
async def test_gemini_parses_sse_stream(gemini_client, monkeypatch):
    """_gemini mem-parse SSE Google AI Studio jadi LLMChunk text + usage."""
    import core.llm_client as llm_mod

    sse_lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"Hai"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":" dunia"}]}}],'
        '"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2}}',
    ]

    class FakeResp:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeStreamCtx()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    texts, usage = [], None
    async for chunk in gemini_client._gemini(
        "gemini-2.0-flash", [{"role": "user", "content": "hi"}], None, 100
    ):
        if chunk.type == "text":
            texts.append(chunk.text)
        elif chunk.type == "usage":
            usage = chunk.usage

    assert "".join(texts) == "Hai dunia"
    assert usage == {"input_tokens": 5, "output_tokens": 2}


@pytest.mark.asyncio
async def test_gemini_sends_tools_as_function_declarations(gemini_client, monkeypatch):
    """Regresi bug: agent Gemini mengklaim menulis PDF tapi tidak pernah memanggil
    tool, karena `tools` sebelumnya TIDAK diteruskan ke _gemini() sama sekali.
    Verifikasi payload sungguhan mengandung functionDeclarations Gemini-style,
    dikonversi dari schema internal Anthropic-style (input_schema -> parameters)."""
    import core.llm_client as llm_mod

    captured_payload = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured_payload.update(json)
            return FakeStreamCtx()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    tools = [
        {
            "name": "pdf_write",
            "description": "Tulis file PDF",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        }
    ]

    async for _ in gemini_client._gemini(
        "gemini-2.5-flash", [{"role": "user", "content": "buat pdf"}], tools, 100
    ):
        pass

    assert "tools" in captured_payload
    decls = captured_payload["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "pdf_write"
    assert decls[0]["parameters"]["required"] == ["path", "content"]


@pytest.mark.asyncio
async def test_gemini_parses_function_call_response(gemini_client, monkeypatch):
    """Response Gemini berisi functionCall -> LLMChunk(type='tool_call', ...)
    dengan tool_input terisi dari 'args' (bukan tool_input={} kosong)."""
    import core.llm_client as llm_mod

    sse_lines = [
        'data: {"candidates":[{"content":{"parts":[{"functionCall":'
        '{"name":"pdf_write","args":{"path":"prd.pdf","content":"isi"}}}]}}]}',
    ]

    class FakeResp:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeStreamCtx()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeClient)

    chunks = []
    async for chunk in gemini_client._gemini(
        "gemini-2.5-flash", [{"role": "user", "content": "buat pdf"}], [{"name": "pdf_write"}], 100
    ):
        chunks.append(chunk)

    tool_calls = [c for c in chunks if c.type == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "pdf_write"
    assert tool_calls[0].tool_input == {"path": "prd.pdf", "content": "isi"}


# ---------- Override mengubah routing di agent_loop ----------


@pytest.mark.asyncio
async def test_override_changes_route_in_agent_loop(db, monkeypatch):
    """Saat override aktif, route.provider/model dipaksa ke pilihan user.

    Kita verifikasi lewat apa yang dikirim ke LLMClient.stream_with_fallback:
    provider & model harus = override, bukan keputusan router otomatis.
    """
    from core.agent_loop import AgentLoop, AgentConfig

    await SettingsStore(db).set_model_override("gemini", "gemini-2.0-flash")

    agent = AgentLoop(AgentConfig(role="pm", session_id="s-ov"), db=db)

    seen = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        seen["provider"] = provider
        seen["model"] = model
        yield LLMChunk(type="text", text="ok")

    agent.llm.stream_with_fallback = fake_stream

    async for _ in agent.run("halo singkat"):
        pass

    assert seen["provider"] == "gemini"
    assert seen["model"] == "gemini-2.0-flash"


@pytest.mark.asyncio
async def test_no_override_uses_router(db, monkeypatch):
    """Tanpa override, provider/model berasal dari router (untuk query pendek = lokal)."""
    from core.agent_loop import AgentLoop, AgentConfig

    agent = AgentLoop(AgentConfig(role="pm", session_id="s-auto"), db=db)

    seen = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        seen["provider"] = provider
        yield LLMChunk(type="text", text="ok")

    agent.llm.stream_with_fallback = fake_stream

    async for _ in agent.run("hi"):
        pass

    # Query "hi" sangat pendek → router pilih tier lokal (ollama), bukan gemini.
    assert seen["provider"] == "ollama"


@pytest.mark.asyncio
async def test_usage_event_carries_token_budget(db):
    """AgentLoop memancarkan event usage berisi context_tokens & max_context_tokens.

    Token budget meter (§1.4) bergantung pada field ini — verifikasi wiring end-to-end.
    """
    from core.agent_loop import AgentLoop, AgentConfig

    agent = AgentLoop(AgentConfig(role="pm", session_id="s-budget"), db=db)

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        yield LLMChunk(type="text", text="jawaban")

    agent.llm.stream_with_fallback = fake_stream

    usage = None
    async for ev in agent.run("halo dunia"):
        if ev.type == "usage":
            usage = ev.usage

    assert usage is not None
    assert "context_tokens" in usage and usage["context_tokens"] > 0
    assert usage["max_context_tokens"] == agent.config.max_context_tokens
    # Context tak boleh melebihi batas (compactor menjaga ini).
    assert usage["context_tokens"] <= usage["max_context_tokens"]
