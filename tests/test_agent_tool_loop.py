"""Test perbaikan tool loop di agent_loop (§ user report: agent menulis file berulang).

Tiga perbaikan yang diuji di sini:
1. Giliran assistant yang MEMANGGIL tool ditulis kembali ke messages (bukan hanya
   hasilnya) — model lokal butuh rekaman itu agar tak memanggil ulang tool sama.
2. `_format_tool_result` mengubah dict hasil jadi teks sukses/gagal yang jelas
   untuk model, bukan repr dict Python.
3. Deteksi loop tulis-file: path yang sama dua kali berturut-turut → hard stop.
"""

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from core.agent_loop import AgentLoop, AgentConfig, _format_tool_result
from core.llm_client import LLMChunk


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


# ── _format_tool_result ──────────────────────────────────────────────────────


def test_format_result_file_write_success_is_terminal():
    """Sukses tulis file → teks eksplisit 'jangan tulis ulang' untuk model lokal."""
    out = _format_tool_result("file_write", {"ok": True, "path": "/ws/hello.go", "bytes": 44})
    assert "SUCCESS" in out
    assert "/ws/hello.go" in out
    assert "do not write it again" in out.lower()


def test_format_result_error_is_clear():
    out = _format_tool_result("file_write", {"error": "path wajib diisi"})
    assert out.startswith("ERROR:")
    assert "path wajib diisi" in out


def test_format_result_generic_ok():
    out = _format_tool_result("list_dir", {"ok": True, "entries": 3})
    assert out.startswith("SUCCESS:")
    assert "entries=3" in out


def test_format_result_non_dict_falls_back():
    assert _format_tool_result("x", "plain string") == "plain string"


# ── Integrasi: writeback giliran assistant + hasil ──────────────────────────


@pytest.mark.asyncio
async def test_assistant_tool_call_written_back_to_messages(db, tmp_path):
    """Setelah tool jalan, messages memuat giliran assistant(tool_calls)+tool, lalu berhenti.

    Memakai file_read (read-only, tanpa approval) agar loop tak menggantung menunggu
    persetujuan — yang diuji di sini adalah STRUKTUR writeback, berlaku untuk tool apa
    pun. Format hasil sukses file_write diuji terpisah (_format_tool_result).
    """
    (tmp_path / "hello.go").write_text("package main")
    agent = AgentLoop(
        AgentConfig(role="dev", session_id="s-wb", workspace_override=str(tmp_path)), db=db
    )

    state = {"hop": 0}
    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        # Snapshot messages TIAP panggilan; capture terakhir = panggilan hop-2
        # (berisi writeback dari tool call hop-1).
        captured["last_messages"] = [dict(m) for m in messages]
        state["hop"] += 1
        if state["hop"] == 1:
            yield LLMChunk(
                type="tool_call",
                tool_name="file_read",
                tool_input={"path": "hello.go"},
            )
        else:
            yield LLMChunk(type="text", text="Isi file sudah saya baca.")

    agent.llm.stream_with_fallback = fake_stream

    events = [ev async for ev in agent.run("Baca file hello world golang")]

    # Model dipanggil 2× (tool → selesai), TIDAK looping.
    assert state["hop"] == 2, f"expected 2 LLM hops, got {state['hop']}"
    # Pada hop kedua, history memuat giliran assistant dengan tool_calls + hasil tool.
    msgs = captured["last_messages"]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in msgs), (
        "giliran assistant yang memanggil tool harus ditulis kembali ke messages"
    )
    assistant_tc = next(m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    assert assistant_tc["tool_calls"][0]["function"]["name"] == "file_read"
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "hasil tool harus ada di messages"
    # Jawaban final ter-stream.
    assert any(ev.type == "token" for ev in events)


@pytest.mark.asyncio
async def test_same_path_write_twice_triggers_loop_stop(db, tmp_path):
    """Menulis path yang SAMA dua kali berturut-turut → loop_stopped (hard stop)."""
    # autopilot=True: tool butuh-approval diantri sebagai proposal (tak blocking tunggu
    # manusia), sehingga loop cepat sampai ke titik deteksi tanpa timeout approval.
    agent = AgentLoop(
        AgentConfig(
            role="dev", session_id="s-loop", workspace_override=str(tmp_path), autopilot=True
        ),
        db=db,
    )

    async def always_write(provider, model, messages, tools=None, max_tokens=4096):
        # Model 'macet' — selalu minta tulis file yang sama (konten sedikit beda tiap kali
        # agar deteksi input-identik lama tak kena; hanya deteksi path yang menangkap).
        n = len([m for m in messages if m.get("role") == "tool"])
        yield LLMChunk(
            type="tool_call",
            tool_name="file_write",
            tool_input={"path": "hello.go", "content": f"package main // {n}"},
        )

    agent.llm.stream_with_fallback = always_write

    events = [ev async for ev in agent.run("Buat file")]
    # Harus berhenti dengan sinyal loop, bukan menulis tanpa batas.
    assert any(ev.type == "status" and ev.text == "loop_stopped" for ev in events), (
        "menulis path sama berulang harus memicu loop_stopped"
    )
