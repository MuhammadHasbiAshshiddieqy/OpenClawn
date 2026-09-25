"""Audit 2026-09-25 (#7-#9): format wire per provider LLM.

Test sebelumnya me-mock `stream_with_fallback` sepenuhnya, jadi tak ada yang
pernah memeriksa payload HTTP sungguhan — tiga bug lolos:
- Claude: tool_call di-yield dengan input {} (input_json_delta diabaikan),
  input_tokens hilang, pesan role "tool" ditolak API (400).
- Gemini: pesan tool dibuang — model tak pernah melihat hasil tool.
- Ollama: schema tool dikirim format Anthropic, bukan {"type":"function",...}.

Di sini `httpx.MockTransport` menangkap request NYATA yang dibangun LLMClient.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

import core.llm_client as lc
from core.llm_client import (
    LLMClient,
    ProviderUnavailable,
    to_anthropic_messages,
    to_gemini_contents,
    to_ollama_messages,
    to_ollama_tools,
)
from infra.config import AppConfig

TOOL_SCHEMA = {
    "name": "file_read",
    "description": "baca file",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}

HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "baca README"},
    {
        "role": "assistant",
        "content": "Saya baca dulu.",
        "tool_calls": [
            {"id": "toolu_1", "function": {"name": "file_read", "arguments": {"path": "README.md"}}}
        ],
    },
    {"role": "tool", "tool_call_id": "toolu_1", "name": "file_read", "content": "isi README"},
]


@pytest.fixture
def capture(monkeypatch):
    """Pasang MockTransport ke client httpx shared; kembalikan dict penangkap."""
    state: dict = {"requests": [], "responses": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["responses"].pop(0)

    monkeypatch.setattr(
        lc, "_shared_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return state


def _client() -> LLMClient:
    vault = AsyncMock()
    vault.get.return_value = "test-key"
    return LLMClient(vault=vault, config=AppConfig(db_path=":memory:"))


def _sse(events: list[dict]) -> httpx.Response:
    body = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, text=body)


# ── Anthropic ────────────────────────────────────────────────────────────────


def test_anthropic_messages_use_tool_blocks():
    msgs = to_anthropic_messages(HISTORY)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assistant = msgs[1]["content"]
    assert assistant[0] == {"type": "text", "text": "Saya baca dulu."}
    assert assistant[1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "file_read",
        "input": {"path": "README.md"},
    }
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert all(m["role"] != "tool" for m in msgs)


def test_anthropic_merges_consecutive_tool_results():
    history = HISTORY[:2] + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "t", "arguments": {}}},
                {"id": "b", "function": {"name": "t", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": "2"},
    ]
    msgs = to_anthropic_messages(history)
    assert msgs[-1]["role"] == "user"
    assert [b["tool_use_id"] for b in msgs[-1]["content"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_claude_stream_assembles_tool_input_and_usage(capture):
    """Reproduksi temuan #7: input_json_delta sebelumnya diabaikan → input {}."""
    capture["responses"].append(
        _sse(
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_9", "name": "file_read"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"README.md"}'},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "usage": {"output_tokens": 7}},
            ]
        )
    )
    chunks = [c async for c in _client()._claude("claude-sonnet-4-6", HISTORY, [TOOL_SCHEMA], 100)]
    calls = [c for c in chunks if c.type == "tool_call"]
    assert calls[0].tool_input == {"path": "README.md"}
    assert calls[0].tool_id == "toolu_9"
    usage = [c for c in chunks if c.type == "usage"][0].usage
    assert usage == {"input_tokens": 42, "output_tokens": 7}

    payload = json.loads(capture["requests"][0].content)
    assert all(m["role"] in ("user", "assistant") for m in payload["messages"])


@pytest.mark.asyncio
async def test_claude_stream_error_event_raises(capture):
    capture["responses"].append(
        _sse([{"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}])
    )
    with pytest.raises(ProviderUnavailable):
        async for _ in _client()._claude("claude-sonnet-4-6", HISTORY, None, 100):
            pass


@pytest.mark.asyncio
async def test_non_transient_4xx_not_retried(capture):
    """400/401 sebelumnya di-retry 3× dengan backoff (CLAUDE.md §3: hanya transien)."""
    capture["responses"].extend([httpx.Response(400, text="bad")] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in _client()._stream_one("anthropic", "claude-sonnet-4-6", HISTORY, None, 10):
            pass
    assert len(capture["requests"]) == 1


# ── Gemini ───────────────────────────────────────────────────────────────────


def test_gemini_contents_include_function_call_and_response():
    """Reproduksi temuan #8: pesan tool & assistant-dengan-tool dibuang."""
    contents = to_gemini_contents(HISTORY)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][1]["functionCall"] == {
        "name": "file_read",
        "args": {"path": "README.md"},
    }
    fr = contents[2]["parts"][0]["functionResponse"]
    assert fr["name"] == "file_read" and fr["response"]["content"] == "isi README"


def test_gemini_thought_signature_roundtrip():
    history = HISTORY[:2] + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "x",
                    "function": {"name": "t", "arguments": {}},
                    "thought_signature": "sig==",
                }
            ],
        }
    ]
    assert to_gemini_contents(history)[1]["parts"][0]["thoughtSignature"] == "sig=="


# ── Ollama ───────────────────────────────────────────────────────────────────


def test_ollama_tools_use_function_format():
    """Reproduksi temuan #9: Ollama butuh {"type":"function","function":{...,"parameters"}}."""
    [tool] = to_ollama_tools([TOOL_SCHEMA])
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "file_read"
    assert tool["function"]["parameters"]["required"] == ["path"]


def test_ollama_messages_tool_name():
    msgs = to_ollama_messages(HISTORY)
    assert msgs[-1] == {"role": "tool", "content": "isi README", "tool_name": "file_read"}
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == {"path": "README.md"}


@pytest.mark.asyncio
async def test_ollama_payload_uses_converted_tools(capture):
    capture["responses"].append(
        httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}) + "\n")
    )
    _ = [c async for c in _client()._ollama("gemma4:e4b", HISTORY, [TOOL_SCHEMA], 100)]
    payload = json.loads(capture["requests"][0].content)
    assert payload["tools"][0]["type"] == "function"
    assert "input_schema" not in json.dumps(payload["tools"])


# ── AgentLoop: semua tool call dalam satu hop dieksekusi ────────────────────


@pytest.mark.asyncio
async def test_agent_loop_executes_all_parallel_tool_calls(tmp_path):
    from core.agent_loop import AgentConfig, AgentLoop
    from core.llm_client import LLMChunk
    from infra.database import DatabaseManager
    from infra.workspace import CURRENT_WORKSPACE_ROOT

    (tmp_path / "a.txt").write_text("AAA")
    (tmp_path / "b.txt").write_text("BBB")
    db = DatabaseManager(AppConfig(db_path=":memory:"))
    conn = await db.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    token = CURRENT_WORKSPACE_ROOT.set(str(tmp_path))
    seen: list[list] = []

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            yield LLMChunk(type="text", text="Baca dua file.")
            yield LLMChunk(
                type="tool_call", tool_name="file_read", tool_input={"path": "a.txt"}, tool_id="t1"
            )
            yield LLMChunk(
                type="tool_call", tool_name="file_read", tool_input={"path": "b.txt"}, tool_id="t2"
            )
        else:
            yield LLMChunk(type="text", text="selesai")

    try:
        agent = AgentLoop(AgentConfig(role="dev", session_id="s-par"), db=db)
        agent.llm.stream_with_fallback = fake_stream
        _ = [ev async for ev in agent.run("baca a dan b")]
    finally:
        CURRENT_WORKSPACE_ROOT.reset(token)
        await db.close()

    second = seen[1]
    assistant = [m for m in second if m.get("tool_calls")][-1]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["t1", "t2"]
    assert assistant["content"] == "Baca dua file."
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["t1", "t2"]
    assert "AAA" in tool_msgs[0]["content"] and "BBB" in tool_msgs[1]["content"]
