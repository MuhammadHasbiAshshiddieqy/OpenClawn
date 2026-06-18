"""Tests untuk ThinkTagSplitter + parsing reasoning per provider (thinking chunk)."""

from unittest.mock import AsyncMock

import pytest

from core.llm_client import LLMClient, ThinkTagSplitter
from infra.config import AppConfig


def _collect(splitter: ThinkTagSplitter, chunks: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for c in chunks:
        out.extend(splitter.feed(c))
    out.extend(splitter.flush())
    return out


def _merge(events: list[tuple[str, str]]) -> dict[str, str]:
    """Gabung teks per-kind agar assertion tak tergantung granularity chunk."""
    merged: dict[str, str] = {}
    for kind, text in events:
        merged[kind] = merged.get(kind, "") + text
    return merged


def test_plain_text_no_think():
    out = _collect(ThinkTagSplitter(), ["halo ", "dunia"])
    assert _merge(out) == {"text": "halo dunia"}


def test_think_then_answer_single_chunk():
    out = _collect(ThinkTagSplitter(), ["<think>nalar</think>jawaban"])
    assert _merge(out) == {"thinking": "nalar", "text": "jawaban"}


def test_tag_split_across_chunks():
    """Tag terpotong di tengah token tidak boleh bocor sebagai teks."""
    out = _collect(ThinkTagSplitter(), ["<thi", "nk>ide", "</thi", "nk>final"])
    m = _merge(out)
    assert m["thinking"] == "ide"
    assert m["text"] == "final"
    # Pastikan tidak ada fragmen tag yang bocor
    assert "<" not in m["text"] and "think" not in m["text"]


def test_close_tag_split_across_chunks():
    out = _collect(ThinkTagSplitter(), ["<think>a", "bc</thin", "k>xyz"])
    m = _merge(out)
    assert m["thinking"] == "abc"
    assert m["text"] == "xyz"


def test_unclosed_think_flushed():
    """Tag <think> tak tertutup → sisa di-flush sebagai thinking (apa adanya)."""
    out = _collect(ThinkTagSplitter(), ["<think>masih mikir"])
    assert _merge(out) == {"thinking": "masih mikir"}


def test_text_before_think():
    out = _collect(ThinkTagSplitter(), ["awal <think>x</think> akhir"])
    m = _merge(out)
    assert m["thinking"] == "x"
    assert m["text"] == "awal  akhir"


def test_no_think_with_angle_bracket():
    """Teks dengan '<' yang bukan tag think tetap utuh."""
    out = _collect(ThinkTagSplitter(), ["if a < b then"])
    assert _merge(out) == {"text": "if a < b then"}


# ── Parsing reasoning per provider → LLMChunk(type="thinking") ───────────────


def _fake_httpx(lines: list[str]):
    """Bangun pengganti httpx.AsyncClient yang men-stream `lines` sebagai aiter_lines."""

    class FakeResp:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for ln in lines:
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

    return FakeClient


@pytest.fixture
def client():
    vault = AsyncMock()
    vault.get = AsyncMock(return_value="fake-key")
    return LLMClient(vault, AppConfig(db_path=":memory:"))


async def _drain(agen):
    think, text = [], []
    async for c in agen:
        if c.type == "thinking":
            think.append(c.text)
        elif c.type == "text":
            text.append(c.text)
    return "".join(think), "".join(text)


async def test_ollama_inline_think_split(client, monkeypatch):
    """Ollama: <think> inline di content → chunk thinking, sisanya text."""
    import core.llm_client as m

    lines = [
        '{"message":{"content":"<think>nalar dulu"}}',
        '{"message":{"content":"</think>jawaban"}}',
        '{"done":true,"prompt_eval_count":3,"eval_count":2}',
    ]
    monkeypatch.setattr(m.httpx, "AsyncClient", _fake_httpx(lines))
    think, text = await _drain(
        client._ollama("deepseek-r1", [{"role": "user", "content": "x"}], None, 100)
    )
    assert think == "nalar dulu"
    assert text == "jawaban"


async def test_ollama_thinking_field(client, monkeypatch):
    """Ollama: field message.thinking terpisah → chunk thinking."""
    import core.llm_client as m

    lines = [
        '{"message":{"thinking":"mikir","content":""}}',
        '{"message":{"content":"hasil"}}',
        '{"done":true,"prompt_eval_count":1,"eval_count":1}',
    ]
    monkeypatch.setattr(m.httpx, "AsyncClient", _fake_httpx(lines))
    think, text = await _drain(
        client._ollama("gpt-oss", [{"role": "user", "content": "x"}], None, 100)
    )
    assert "mikir" in think
    assert text == "hasil"


async def test_anthropic_thinking_delta(client, monkeypatch):
    """Anthropic: thinking_delta → chunk thinking; text_delta → text."""
    import core.llm_client as m

    lines = [
        'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"langkah 1"}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"jawab"}}',
    ]
    monkeypatch.setattr(m.httpx, "AsyncClient", _fake_httpx(lines))
    think, text = await _drain(
        client._claude("claude-x", [{"role": "user", "content": "x"}], None, 100)
    )
    assert think == "langkah 1"
    assert text == "jawab"


async def test_gemini_thought_part(client, monkeypatch):
    """Gemini: part.thought=true → chunk thinking; selain itu text."""
    import core.llm_client as m

    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"nalar","thought":true}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"final"}]}}]}',
    ]
    monkeypatch.setattr(m.httpx, "AsyncClient", _fake_httpx(lines))
    think, text = await _drain(
        client._gemini("gemini-2.5-pro", [{"role": "user", "content": "x"}], 100)
    )
    assert think == "nalar"
    assert text == "final"
