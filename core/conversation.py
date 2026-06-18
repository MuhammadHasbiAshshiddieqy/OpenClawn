"""Multi-agent conversation — beberapa agent (role) saling mengobrol.

Ide inti: percakapan = urutan giliran agent; sebuah `TurnStrategy` memutuskan SIAPA
bicara berikutnya & KAPAN berhenti. Tiga pola (pipeline / debate / orchestrator) =
tiga strategy di atas satu `ConversationOrchestrator`.

Modul ini extractable (CLAUDE.md §1.6): hanya bergantung pada `DatabaseManager`,
`AppConfig`, dan `agent_factory` callable — tidak ada import web. Tiap giliran adalah
`AgentLoop.run()` penuh (tool/memory/routing/crystallization tetap jalan per agent).
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from core.agent_loop import AgentLoop
from infra.config import CONFIG, AppConfig
from infra.database import DatabaseManager
from infra.logging import log
from roles.contracts import CONTRACT_REGISTRY
from roles.registry import parse_contract

# Factory: role → AgentLoop baru (di-inject web layer, reuse db & approval gate).
AgentFactory = Callable[[str], AgentLoop]


@dataclass
class ConversationEvent:
    """Event yang di-stream ke UI. Sibling `AgentEvent`, menambah role + boundary.

    `type`:
      - "turn"             → penanda mulai giliran (frontend buka bubble baru berlabel role)
      - "token" / "status" → di-rewrap dari AgentEvent giliran aktif
      - "conversation_end" → akhir percakapan; `detail` = alasan, `usage` = total agregat
    """

    type: str
    role: str = ""
    text: str = ""
    detail: str = ""
    turn_index: int = 0
    usage: dict | None = None


@dataclass
class ConversationState:
    """State berbagi lintas giliran."""

    # (role, content) berurutan — termасuk "user" untuk pesan awal & interjection.
    transcript: list[tuple[str, str]] = field(default_factory=list)
    last_output: dict | None = None  # contract tervalidasi terakhir (atau {"text": raw})
    turn_index: int = 0
    round_index: int = 0


class ConversationControl:
    """Kontrol STOP + INTERJECT, web-agnostic.

    Dibuat per session di web layer; STOP via flag/disconnect, INTERJECT via antrian
    pesan user yang disuntik ke giliran berikutnya.
    """

    def __init__(self, disconnect_check: Callable[[], Awaitable[bool]] | None = None):
        self._stopped = False
        self._interjections: list[str] = []
        self._disconnect_check = disconnect_check

    def stop(self) -> None:
        self._stopped = True

    def add_interjection(self, text: str) -> None:
        if text and text.strip():
            self._interjections.append(text.strip())

    def pop_interjection(self) -> str | None:
        return self._interjections.pop(0) if self._interjections else None

    async def is_stopped(self) -> bool:
        if self._stopped:
            return True
        if self._disconnect_check is not None:
            try:
                return await self._disconnect_check()
            except Exception:  # noqa: BLE001 — disconnect check tak boleh menjatuhkan loop
                return False
        return False


# ── Strategies ──────────────────────────────────────────────────────────────


class TurnStrategy(ABC):
    """Menentukan siapa bicara berikutnya & bagaimana input giliran dirakit."""

    participants: list[str]

    @abstractmethod
    def next_speaker(self, state: ConversationState) -> str | None:
        """Role berikutnya, atau None bila percakapan selesai."""

    def build_turn_input(
        self, state: ConversationState, role: str, interjection: str | None
    ) -> str:
        """Rakit prompt untuk giliran ini. Default: konten terakhir + interjection."""
        parts: list[str] = []
        if state.transcript:
            last_role, last_content = state.transcript[-1]
            parts.append(f"[{last_role.upper()}]: {last_content}")
        if interjection:
            parts.append(f"[USER menyela]: {interjection}")
        return "\n\n".join(parts) if parts else ""

    def wants_contract(self, role: str) -> bool:
        return False


class PipelineStrategy(TurnStrategy):
    """Handoff berurutan sekali jalan: pm → dev → qa (default)."""

    def __init__(self, participants: list[str]):
        self.participants = participants

    def next_speaker(self, state: ConversationState) -> str | None:
        # transcript[0] = pesan user awal; tiap giliran agent menambah 1 entri.
        done = state.turn_index
        if done >= len(self.participants):
            return None
        return self.participants[done]

    def build_turn_input(
        self, state: ConversationState, role: str, interjection: str | None
    ) -> str:
        parts = [
            f"Anda adalah role '{role}' dalam pipeline {' → '.join(self.participants)}.",
        ]
        # Suapkan tujuan awal + output role sebelumnya (tervalidasi bila ada).
        user_goal = next((c for r, c in state.transcript if r == "user"), "")
        if user_goal:
            parts.append(f"Tujuan dari user: {user_goal}")
        if state.last_output:
            prev = json.dumps(state.last_output, ensure_ascii=False)
            parts.append(f"Output role sebelumnya:\n{prev}")
        if interjection:
            parts.append(f"[USER menyela]: {interjection}")
        return "\n\n".join(parts)

    def wants_contract(self, role: str) -> bool:
        return role in CONTRACT_REGISTRY


class DebateStrategy(TurnStrategy):
    """Round-robin bebas selama `rounds` siklus penuh."""

    def __init__(self, participants: list[str], rounds: int):
        self.participants = participants
        self.rounds = max(1, rounds)

    def next_speaker(self, state: ConversationState) -> str | None:
        if state.turn_index >= self.rounds * len(self.participants):
            return None
        return self.participants[state.turn_index % len(self.participants)]

    def build_turn_input(
        self, state: ConversationState, role: str, interjection: str | None
    ) -> str:
        transcript = "\n".join(f"[{r.upper()}]: {c}" for r, c in state.transcript)
        parts = [
            f"Diskusi multi-peran. Anda role '{role}'. Tanggapi diskusi sejauh ini, "
            "berikan sudut pandang peran Anda secara ringkas.",
            f"Diskusi sejauh ini:\n{transcript}",
        ]
        if interjection:
            parts.append(f"[USER menyela]: {interjection}")
        return "\n\n".join(parts)


class OrchestratorStrategy(TurnStrategy):
    """Lead mendelegasikan ke worker secara DINAMIS.

    Setelah giliran lead, parse output untuk directive JSON:
      {"delegate_to": "<role>", "task": "..."}  → giliran worker itu
      {"done": true}                            → selesai
    Bila tak terbaca → FALLBACK alur tetap: lead → tiap worker → lead sintesis → selesai.
    """

    def __init__(self, lead: str, workers: list[str]):
        self.lead = lead
        self.workers = workers
        self.participants = [lead, *workers]
        self._pending: list[str] = []  # role yang akan bicara (dinamis)
        self._fallback = False
        self._fallback_queue: list[str] = []

    def next_speaker(self, state: ConversationState) -> str | None:
        # Giliran pertama selalu lead.
        if state.turn_index == 0:
            return self.lead

        if self._fallback:
            return self._fallback_queue.pop(0) if self._fallback_queue else None

        # Siapa bicara terakhir? (entri agent terakhir di transkrip, abaikan "user")
        last_role = next((r for r, _ in reversed(state.transcript) if r != "user"), self.lead)

        # Setelah WORKER bicara → kembali ke LEAD untuk meninjau & memutuskan.
        if last_role != self.lead:
            return self.lead

        # Setelah LEAD bicara → ikuti directive-nya (delegasi / selesai).
        directive = self._parse_directive(state)
        if directive is None:
            # Tak terbaca → fallback alur tetap: semua worker lalu lead sintesis.
            self._fallback = True
            self._fallback_queue = [*self.workers, self.lead]
            log.info("orchestrator_fallback", reason="directive_unparseable")
            return self._fallback_queue.pop(0) if self._fallback_queue else None

        if directive.get("done"):
            return None
        target = directive.get("delegate_to")
        if target in self.workers:
            return target
        # delegate_to tak valid / menunjuk lead → anggap selesai.
        return None

    def build_turn_input(
        self, state: ConversationState, role: str, interjection: str | None
    ) -> str:
        user_goal = next((c for r, c in state.transcript if r == "user"), "")
        if role == self.lead and state.turn_index == 0:
            parts = [
                f"Anda LEAD orchestrator. Pecah tugas & delegasikan ke worker: {self.workers}.",
                f"Tugas user: {user_goal}",
                "Akhiri jawaban dengan directive JSON satu baris: "
                '{"delegate_to":"<role>","task":"..."} atau {"done":true}.',
            ]
        elif role == self.lead:
            parts = [
                "Anda LEAD. Tinjau hasil worker di transkrip, lalu lanjut delegasikan "
                "atau simpulkan.",
                'Akhiri dengan directive JSON: {"delegate_to":"<role>","task":"..."} '
                'atau {"done":true}.',
            ]
        else:
            # Worker: kerjakan task dari directive lead terakhir.
            task = self._last_task(state) or user_goal
            parts = [f"Anda worker '{role}'. Kerjakan tugas dari lead:", task]
        if interjection:
            parts.append(f"[USER menyela]: {interjection}")
        transcript = "\n".join(f"[{r.upper()}]: {c[:400]}" for r, c in state.transcript)
        if transcript:
            parts.append(f"Konteks:\n{transcript}")
        return "\n\n".join(parts)

    def _parse_directive(self, state: ConversationState) -> dict | None:
        """Ambil JSON directive dari output lead terakhir (cari objek JSON terakhir)."""
        for role, content in reversed(state.transcript):
            if role != self.lead:
                continue
            start = content.rfind("{")
            end = content.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                d = json.loads(content[start : end + 1])
                return d if isinstance(d, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _last_task(self, state: ConversationState) -> str | None:
        d = self._parse_directive(state)
        return d.get("task") if d else None


# ── Orchestrator ──────────────────────────────────────────────────────────────


class ConversationOrchestrator:
    """Menjalankan loop percakapan multi-agent sampai strategy/cap/STOP menghentikannya."""

    def __init__(
        self,
        strategy: TurnStrategy,
        db: DatabaseManager,
        agent_factory: AgentFactory,
        session_id: str,
        config: AppConfig = CONFIG,
        control: ConversationControl | None = None,
    ):
        self.strategy = strategy
        self.db = db
        self.agent_factory = agent_factory
        self.session_id = session_id
        self.config = config
        self.control = control or ConversationControl()

    async def run(self, initial_message: str):
        """Yield ConversationEvent. Tiap giliran = AgentLoop.run() penuh."""
        state = ConversationState(transcript=[("user", initial_message)])
        # Akumulasi biaya lintas-giliran → ditampilkan di akhir percakapan.
        # context: PEAK (bukan jumlah) — tiap giliran context window independen,
        # menjumlahkan tak bermakna; yang penting giliran terberat vs batas.
        totals = {
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "latency_ms": 0,
            "turns": 0,
            "peak_context_tokens": 0,
            "max_context_tokens": self.config.max_context_tokens,
        }

        while state.turn_index < self.config.max_conversation_turns:
            if await self.control.is_stopped():
                yield ConversationEvent("conversation_end", detail="stopped", usage=totals)
                return

            role = self.strategy.next_speaker(state)
            if role is None:
                yield ConversationEvent("conversation_end", detail="strategy_done", usage=totals)
                return

            interjection = self.control.pop_interjection()
            if interjection:
                # Tampilkan interjection sebagai bagian transkrip (agar tercatat).
                state.transcript.append(("user", interjection))
            turn_input = self.strategy.build_turn_input(state, role, interjection)

            ti = state.turn_index
            yield ConversationEvent("turn", role=role, text=role.upper(), turn_index=ti)

            agent = self.agent_factory(role)
            collected = ""
            stopped_mid = False
            async for ev in agent.run(turn_input):
                if await self.control.is_stopped():
                    stopped_mid = True
                    break
                # Usage bukan untuk ditampilkan per-token — akumulasi diam-diam.
                if ev.type == "usage" and ev.usage:
                    totals["tokens_in"] += ev.usage.get("tokens_in", 0)
                    totals["tokens_out"] += ev.usage.get("tokens_out", 0)
                    totals["cost_usd"] += ev.usage.get("cost_usd", 0.0)
                    totals["latency_ms"] += ev.usage.get("latency_ms", 0)
                    totals["peak_context_tokens"] = max(
                        totals["peak_context_tokens"], ev.usage.get("context_tokens", 0)
                    )
                    totals["turns"] += 1
                    continue
                if ev.type == "token":
                    collected += ev.text
                yield ConversationEvent(
                    ev.type, role=role, text=ev.text, detail=ev.detail, turn_index=ti
                )

            if stopped_mid:
                yield ConversationEvent(
                    "conversation_end", role=role, detail="stopped", turn_index=ti, usage=totals
                )
                return

            # Contract validation (Inovasi 4) — degrade graceful bila gagal.
            if self.strategy.wants_contract(role):
                state.last_output = await self._record_handoff(role, turn_input, collected)
            else:
                state.last_output = {"text": collected}

            state.transcript.append((role, collected))
            state.turn_index += 1
            state.round_index = state.turn_index // max(1, len(self.strategy.participants))

        yield ConversationEvent("conversation_end", detail="max_turns", usage=totals)

    async def _record_handoff(self, role: str, task_input: str, raw: str) -> dict:
        """Validasi output role vs contract, tulis ke role_handoffs. Gagal → teks mentah."""
        contract_cls = CONTRACT_REGISTRY.get(role)
        if not contract_cls:
            return {"text": raw}
        validated, ok = parse_contract(raw, contract_cls)
        try:
            await self.db.execute(
                """INSERT INTO role_handoffs (session_id, from_role, to_role, task_input,
                                              contract_name, output_json, validation_ok)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    self.session_id,
                    "conversation",
                    role,
                    task_input[:1000],
                    role,
                    json.dumps(validated, ensure_ascii=False),
                    int(ok),
                ),
            )
        except Exception as e:  # noqa: BLE001 — audit gagal jangan jatuhkan percakapan
            log.error("handoff_log_failed", role=role, error=str(e), session=self.session_id)
        # Degrade: bila tak valid, teruskan teks mentah ke role berikutnya.
        return validated if ok else {"text": raw}


def make_strategy(
    pattern: str,
    participants: list[str] | None,
    rounds: int,
    config: AppConfig = CONFIG,
) -> TurnStrategy:
    """Bangun strategy dari parameter request. Default participants dari config."""
    parts = participants or list(config.conversation_default_participants)
    match pattern:
        case "pipeline":
            return PipelineStrategy(parts)
        case "debate":
            return DebateStrategy(parts, rounds or config.debate_default_rounds)
        case "orchestrator":
            lead = parts[0]
            workers = parts[1:] or ["dev", "qa"]
            return OrchestratorStrategy(lead, workers)
        case _:
            raise ValueError(f"pattern tidak dikenal: {pattern}")
