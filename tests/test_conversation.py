"""Test multi-agent conversation: strategy, orchestrator loop, stop, interject,
contract validation. DB :memory:, tanpa LLM nyata (fake agent_factory)."""

import json

import pytest

from core.agent_loop import AgentEvent
from core.conversation import (
    ConversationControl,
    ConversationOrchestrator,
    ConversationState,
    DebateStrategy,
    OrchestratorStrategy,
    PipelineStrategy,
    make_strategy,
)
from infra.config import AppConfig
from infra.database import DatabaseManager


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


class FakeAgent:
    """Agent palsu: run() yield AgentEvent skrip, dan catat prompt yang diterima."""

    def __init__(self, role: str, reply: str, calls: list):
        self.role = role
        self.reply = reply
        self.calls = calls  # daftar bersama (role, prompt) untuk assertion

    async def run(self, prompt: str):
        self.calls.append((self.role, prompt))
        yield AgentEvent(type="status", text="thinking")
        yield AgentEvent(type="token", text=self.reply)


def make_factory(replies: dict[str, str], calls: list):
    """agent_factory: role → FakeAgent dengan balasan dari `replies` (default 'ok')."""

    def factory(role: str):
        return FakeAgent(role, replies.get(role, f"jawaban {role}"), calls)

    return factory


async def _collect(orch, message):
    return [ev async for ev in orch.run(message)]


# ── Pipeline ──────────────────────────────────────────────────────────────────


async def test_pipeline_strategy_orders_roles(db):
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev", "qa"]),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-pipe",
    )
    events = await _collect(orch, "bangun fitur login")
    roles_spoken = [c[0] for c in calls]
    assert roles_spoken == ["pm", "dev", "qa"]
    end = [e for e in events if e.type == "conversation_end"]
    assert end and end[-1].detail == "strategy_done"


async def test_turn_boundary_events_emitted(db):
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev", "qa"]),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-turn",
    )
    events = await _collect(orch, "x")
    turn_events = [e for e in events if e.type == "turn"]
    assert [e.role for e in turn_events] == ["pm", "dev", "qa"]
    assert [e.turn_index for e in turn_events] == [0, 1, 2]


# ── Debate ──────────────────────────────────────────────────────────────────


async def test_debate_strategy_round_robin(db):
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=DebateStrategy(["pm", "dev"], rounds=2),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-debate",
    )
    await _collect(orch, "diskusikan scope")
    assert [c[0] for c in calls] == ["pm", "dev", "pm", "dev"]


# ── Orchestrator (dinamis + fallback) ─────────────────────────────────────────


async def test_orchestrator_dynamic_delegation(db):
    calls: list = []

    # Lead pertama delegasi ke dev; lead kedua → done. FakeAgent statis, jadi
    # pakai factory dinamis yang mengubah balasan lead sesuai pemanggilan ke-n.
    class LeadThenDone:
        def __init__(self, calls):
            self.calls = calls
            self.n = 0

        def __call__(self, role):
            if role == "pm":
                self.n += 1
                reply = (
                    'Rencana awal. {"delegate_to":"dev","task":"implement"}'
                    if self.n == 1
                    else 'Selesai. {"done":true}'
                )
                return FakeAgent("pm", reply, self.calls)
            return FakeAgent(role, f"hasil {role}", self.calls)

    orch = ConversationOrchestrator(
        strategy=OrchestratorStrategy(lead="pm", workers=["dev", "qa"]),
        db=db,
        agent_factory=LeadThenDone(calls),
        session_id="s-orch",
    )
    await _collect(orch, "tugas kompleks")
    spoken = [c[0] for c in calls]
    assert spoken[0] == "pm"  # lead dulu
    assert "dev" in spoken  # worker yang didelegasi
    assert spoken[-1] == "pm"  # lead menyimpulkan (done)


async def test_orchestrator_fallback_when_unparseable(db):
    calls: list = []
    # lead tak pernah mengeluarkan JSON directive → fallback: lead → dev → qa → lead
    orch = ConversationOrchestrator(
        strategy=OrchestratorStrategy(lead="pm", workers=["dev", "qa"]),
        db=db,
        agent_factory=make_factory({"pm": "teks bebas tanpa json"}, calls),
        session_id="s-orch-fb",
    )
    await _collect(orch, "tugas")
    spoken = [c[0] for c in calls]
    assert spoken[0] == "pm"
    # fallback queue = workers + lead
    assert set(["dev", "qa"]).issubset(set(spoken))


# ── Cap, stop, interject ──────────────────────────────────────────────────────


async def test_max_conversation_turns_respected(db):
    calls: list = []
    cfg = AppConfig(db_path=":memory:", max_conversation_turns=3)
    # debate rounds besar → akan kena cap, bukan selesai natural
    orch = ConversationOrchestrator(
        strategy=DebateStrategy(["pm", "dev"], rounds=99),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-cap",
        config=cfg,
    )
    events = await _collect(orch, "x")
    assert len(calls) == 3
    assert events[-1].type == "conversation_end" and events[-1].detail == "max_turns"


async def test_stop_halts_between_turns(db):
    calls: list = []
    control = ConversationControl()

    # Stop dipicu lewat disconnect_check setelah giliran pertama selesai.
    state = {"turns_seen": 0}

    async def disconnect():
        # Berhenti begitu giliran pertama sudah berjalan.
        return state["turns_seen"] >= 1

    control._disconnect_check = disconnect

    class CountingFactory:
        def __call__(self, role):
            state["turns_seen"] += 1
            return FakeAgent(role, "ok", calls)

    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev", "qa"]),
        db=db,
        agent_factory=CountingFactory(),
        session_id="s-stop",
        control=control,
    )
    events = await _collect(orch, "x")
    ends = [e for e in events if e.type == "conversation_end"]
    assert ends and ends[-1].detail == "stopped"
    assert len(calls) <= 2  # tidak menjalankan seluruh 3 giliran


async def test_interject_consumed_in_next_turn(db):
    calls: list = []
    control = ConversationControl()
    control.add_interjection("PERHATIKAN KEAMANAN")
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev", "qa"]),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-interject",
        control=control,
    )
    await _collect(orch, "bangun fitur")
    # Interjection di-pop di giliran pertama (pm) → harus muncul di prompt-nya.
    pm_prompt = next(p for r, p in calls if r == "pm")
    assert "PERHATIKAN KEAMANAN" in pm_prompt


# ── Contract validation (degrade graceful) ────────────────────────────────────


async def test_pipeline_contract_valid(db):
    calls: list = []
    valid_pm = json.dumps({"summary": "ringkasan", "priority": "high"})
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm"]),
        db=db,
        agent_factory=make_factory({"pm": valid_pm}, calls),
        session_id="s-valid",
    )
    await _collect(orch, "x")
    row = await db.fetchone("SELECT validation_ok FROM role_handoffs WHERE session_id='s-valid'")
    assert row and row["validation_ok"] == 1


async def test_pipeline_contract_degrades_on_garbage(db):
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev"]),
        db=db,
        agent_factory=make_factory({"pm": "ini bukan json sama sekali"}, calls),
        session_id="s-degrade",
    )
    events = await _collect(orch, "x")
    # validation_ok=0 dicatat TAPI pipeline lanjut ke dev.
    row = await db.fetchone(
        "SELECT validation_ok FROM role_handoffs WHERE session_id='s-degrade' AND to_role='pm'"
    )
    assert row and row["validation_ok"] == 0
    assert [c[0] for c in calls] == ["pm", "dev"]  # tidak berhenti di pm
    assert events[-1].detail == "strategy_done"


# ── Control unit ──────────────────────────────────────────────────────────────


def test_control_interjection_queue():
    c = ConversationControl()
    c.add_interjection("  ")  # kosong diabaikan
    c.add_interjection("halo")
    assert c.pop_interjection() == "halo"
    assert c.pop_interjection() is None


async def test_state_dataclass_defaults():
    s = ConversationState(transcript=[("user", "x")])
    assert s.turn_index == 0 and s.last_output is None


# ── make_strategy: participants & lead fleksibel dari UI ──────────────────────


def test_make_strategy_pipeline_preserves_order():
    """Pipeline memakai urutan participants apa adanya (urutan handoff)."""
    s = make_strategy("pipeline", ["dev", "qa"], 0)
    assert isinstance(s, PipelineStrategy)
    assert s.participants == ["dev", "qa"]


def test_make_strategy_debate_uses_rounds():
    """Debate meneruskan jumlah ronde dari UI."""
    s = make_strategy("debate", ["pm", "qa"], 3)
    assert isinstance(s, DebateStrategy)
    assert s.rounds == 3 and s.participants == ["pm", "qa"]


def test_make_strategy_orchestrator_lead_is_first_participant():
    """Lead = participant pertama → bukan harus PM. Worker = sisanya."""
    s = make_strategy("orchestrator", ["dev", "pm", "qa"], 0)
    assert isinstance(s, OrchestratorStrategy)
    assert s.lead == "dev"
    assert s.workers == ["pm", "qa"]
    assert s.participants == ["dev", "pm", "qa"]


def test_make_strategy_orchestrator_default_pm_lead():
    """Tanpa participants → default config; PM tetap lead default."""
    s = make_strategy("orchestrator", None, 0)
    assert isinstance(s, OrchestratorStrategy)
    assert s.lead == "pm"  # default config.conversation_default_participants[0]


def test_make_strategy_unknown_pattern_raises():
    with pytest.raises(ValueError):
        make_strategy("nonsense", ["pm"], 0)


async def test_orchestrator_non_pm_lead_runs(db):
    """End-to-end: orchestrator dengan lead 'dev' menjalankan dev lebih dulu."""
    calls: list = []

    def factory(role: str):
        # Lead (dev) langsung selesai agar percakapan singkat & deterministik.
        reply = '{"done": true}' if role == "dev" else "ok"
        return FakeAgent(role, reply, calls)

    orch = ConversationOrchestrator(
        strategy=make_strategy("orchestrator", ["dev", "pm", "qa"], 0),
        db=db,
        agent_factory=factory,
        session_id="s-leaddev",
    )
    await _collect(orch, "kerjakan sesuatu")
    assert calls[0][0] == "dev"  # lead (dev) bicara pertama


# ── Persistensi percakapan (arsip multi-agent) ────────────────────────────────


async def test_conversation_persisted_on_completion(db):
    """Percakapan selesai → satu baris tersimpan di conversations dengan transkrip."""
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm", "dev", "qa"]),
        db=db,
        agent_factory=make_factory({"pm": "rencana", "dev": "kode", "qa": "lulus"}, calls),
        session_id="s-persist",
        pattern="pipeline",
    )
    await _collect(orch, "bangun fitur X")

    row = await db.fetchone("SELECT * FROM conversations WHERE session_id='s-persist'")
    assert row is not None
    assert row["pattern"] == "pipeline"
    assert row["participants"] == "pm,dev,qa"
    assert row["end_reason"] == "strategy_done"
    transcript = json.loads(row["transcript_json"])
    # transcript: [user, pm, dev, qa]
    assert transcript[0] == ["user", "bangun fitur X"]
    spoken = [t[0] for t in transcript]
    assert spoken == ["user", "pm", "dev", "qa"]


async def test_conversation_persisted_once_per_run(db):
    """Tepat satu baris arsip per run (persist hanya di conversation_end)."""
    calls: list = []
    orch = ConversationOrchestrator(
        strategy=PipelineStrategy(["pm"]),
        db=db,
        agent_factory=make_factory({}, calls),
        session_id="s-once",
        pattern="pipeline",
    )
    await _collect(orch, "halo")
    rows = await db.fetchall("SELECT id FROM conversations WHERE session_id='s-once'")
    assert len(rows) == 1
