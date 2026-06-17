import asyncio
import time
import tomllib
from dataclasses import dataclass, field
from typing import AsyncGenerator

from infra.config import AppConfig, CONFIG
from infra.database import DatabaseManager
from infra.logging import log
from infra.settings import SettingsStore
from core.router import SmartRouter
from core.audit import RoutingAuditor
from core.compactor import ContextCompactor
from core.crystallizer import ConfidenceCrystallizer
from core.llm_client import LLMClient
from memory.layers import MemoryManager
from memory.skill_decay import SkillDecayManager
from tools import TOOL_REGISTRY
from security.vault import Vault
from security.approval import ApprovalGate
from security.shield import Shield


@dataclass
class AgentConfig:
    role: str
    session_id: str
    user_id: str = "default"


@dataclass
class Turn:
    role: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model_used: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0
    fallback_used: bool = False


class AgentLoop:
    def __init__(
        self,
        agent_cfg: AgentConfig,
        db: DatabaseManager,
        config: AppConfig = CONFIG,
        approval: ApprovalGate | None = None,
    ):
        self.cfg = agent_cfg
        self.config = config
        self.db = db
        self.vault = Vault()
        self.llm = LLMClient(self.vault, config)
        self.memory = MemoryManager(agent_cfg.role, agent_cfg.session_id, db)
        self.decay = SkillDecayManager(agent_cfg.role, db, config)
        self.router = SmartRouter(role=agent_cfg.role)
        self.auditor = RoutingAuditor(db)
        self.compactor = ContextCompactor(config.max_context_tokens)
        self.crystallizer = ConfidenceCrystallizer(agent_cfg.role, self.llm, db)
        # ApprovalGate di-inject dari Web UI agar resolve() mengenai Future yang sama
        # (AgentLoop dibuat baru per request, tapi approval harus shared antar request).
        self.approval = approval or ApprovalGate(db, config)
        self.shield = Shield()
        self.settings = SettingsStore(db)
        self.history: list[Turn] = []

        # nit #2: cache soul.toml sekali, jangan baca tiap turn
        self._soul = self._load_soul_once()

    def _load_soul_once(self) -> dict:
        with open(f"roles/{self.cfg.role}/soul.toml", "rb") as f:
            return tomllib.load(f)

    async def run(self, user_message: str) -> AsyncGenerator[str, None]:
        start = time.monotonic()

        # 0. Shield: scan input (lapisan kosmetik, BUKAN pertahanan utama — lihat §17).
        # Pertahanan utama tetap container isolation di code_run.
        safe, reason = self.shield.scan_input(user_message)
        if not safe:
            log.warning("shield_blocked_input", session=self.cfg.session_id, reason=reason)
            yield reason
            return

        # 1. Deteksi koreksi user (audit feedback) [#1]
        if self.history:
            await self.auditor.check_correction(user_message, self.cfg.session_id)

        # 2. Load skill aktif (belum decayed) [#2]
        active_skills = await self.decay.get_active_skills(query=user_message)

        # 3. Memory context
        memory_ctx = await self.memory.load_context(user_message, active_skills)

        # 4. Build messages
        messages = self.compactor.build(
            soul=self._soul["system_prompt"]["content"],
            memory=memory_ctx,
            history=self.history,
            user_message=user_message,
        )

        # 5. Route (soul-aware) + log [#1]
        route = self.router.decide(messages, user_message)
        # Override model dari /settings: pilihan sadar user untuk memaksa 1 model
        # (mis. Gemini) melewati keputusan otomatis. Audit tetap mencatat keputusan
        # router ASLI di reason agar transparansi routing tidak hilang.
        override = await self.settings.get_model_override()
        if override:
            ov_provider, ov_model = override
            route.reason = f"[override→{ov_provider}:{ov_model}] {route.reason}"
            route.provider = ov_provider
            route.model = ov_model
            route.cost_per_1k = 0.0  # biaya nyata model override tak dipetakan; jangan tebak
        event_id = await self.auditor.log_decision(
            self.cfg.session_id, self.cfg.role, user_message, route
        )

        # 6. Iterative tool loop (audit #10: bukan rekursif)
        turn = Turn(role="assistant", model_used=route.model)
        tools_schema = self._tools_for_role()

        async for chunk in self._run_tool_loop(messages, route, tools_schema, turn):
            yield chunk

        # 7. Finalize
        turn.latency_ms = int((time.monotonic() - start) * 1000)
        turn.cost_usd = route.cost_per_1k * (turn.tokens_in + turn.tokens_out) / 1000
        self.history.append(Turn(role="user", content=user_message))
        self.history.append(turn)
        await self.auditor.finalize(event_id, turn)

        # 8. Post-turn dengan error handling (audit #3: bukan fire-and-forget)
        # Snapshot history sebelum create_task — hindari race condition jika history berubah
        history_snapshot = list(self.history)
        task = asyncio.create_task(
            self._post_turn(user_message, turn, active_skills, history_snapshot)
        )
        task.add_done_callback(self._post_turn_done)

    def _post_turn_done(self, task: asyncio.Task) -> None:
        """Audit #3: log error jika background task gagal, jangan hilang diam-diam."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("post_turn_failed", error=str(exc), session=self.cfg.session_id)

    async def _run_tool_loop(
        self, messages: list, route, tools_schema: list, turn: Turn
    ) -> AsyncGenerator[str, None]:
        """Audit #10: iterative, bukan rekursif."""
        hop = 0
        while hop <= self.config.max_tool_hops:
            pending_tool = None
            async for chunk in self.llm.stream_with_fallback(
                route.provider, route.model, messages, tools_schema
            ):
                if chunk.type == "text":
                    turn.content += chunk.text
                    yield chunk.text
                elif chunk.type == "tool_call":
                    pending_tool = chunk
                elif chunk.type == "usage":
                    turn.tokens_in = chunk.usage.get("input_tokens", 0)
                    turn.tokens_out = chunk.usage.get("output_tokens", 0)
                elif chunk.type == "fallback" and chunk.fallback_used:
                    turn.fallback_used = True

            if not pending_tool:
                break  # tidak ada tool call → selesai

            result = await self._execute_tool(pending_tool.tool_name, pending_tool.tool_input)
            turn.tool_calls.append(
                {"name": pending_tool.tool_name, "input": pending_tool.tool_input}
            )
            messages.append(
                {"role": "tool", "name": pending_tool.tool_name, "content": str(result)}
            )
            hop += 1

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        tool = TOOL_REGISTRY.get(name)
        if not tool:
            return {"error": f"Tool '{name}' tidak ditemukan"}

        if not self._tool_allowed(name):
            return {"error": f"Tool '{name}' tidak diizinkan untuk role {self.cfg.role}"}

        if tool.requires_approval:
            approved = await self.approval.request(self.cfg.session_id, name, input_data)
            if not approved:
                return {"error": f"Tool '{name}' ditolak oleh user"}

        return await tool.execute(input_data, vault=self.vault)

    def _tool_allowed(self, name: str) -> bool:
        return name in self._soul.get("tools", {}).get("allowed", [])

    def _tools_for_role(self) -> list:
        """Nit #1: hanya kirim schema tool yang diizinkan ke LLM."""
        allowed = set(self._soul.get("tools", {}).get("allowed", []))
        return [t.schema() for n, t in TOOL_REGISTRY.items() if n in allowed]

    async def _post_turn(
        self, user_message: str, turn: Turn, active_skills: list, history_snapshot: list
    ) -> None:
        """Background: tulis memori + decay pass + crystallize jika syarat terpenuhi."""
        # Memori L1: checkpoint state terakhir tiap turn agar turn berikut punya konteks
        # ringkas tanpa memuat seluruh history (token-first, §1.4).
        if turn.content:
            await self.memory.update_checkpoint(turn.content)

        # Memori L4: arsipkan sesi yang sudah cukup panjang untuk cross-session search.
        # Tidak tiap turn — hanya saat bermakna (ambang archive_after_turns).
        if len(history_snapshot) >= self.config.archive_after_turns:
            await self.memory.archive_session(
                summary=turn.content[:200],
                full_content=self._render_history(history_snapshot),
            )

        await self.decay.maybe_run_decay_pass()
        if self.crystallizer.should_attempt(history_snapshot):
            await self.crystallizer.crystallize(
                task=user_message,
                solution=turn.content,
                history=history_snapshot,
                generator_model=turn.model_used,
            )

    @staticmethod
    def _render_history(history: list[Turn]) -> str:
        """Serialisasi history jadi teks untuk arsip L4 (full_content yang bisa di-search)."""
        return "\n".join(f"{t.role}: {t.content}" for t in history if t.content)
