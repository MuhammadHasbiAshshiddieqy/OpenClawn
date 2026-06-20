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
from core.calibration import CalibrationStore
from core.router_config import RouterConfigStore
from core.tool_audit import ToolAudit
from core.compactor import ContextCompactor
from core.crystallizer import ConfidenceCrystallizer
from core.llm_client import LLMClient
from memory.layers import MemoryManager
from memory.skill_decay import SkillDecayManager
from memory.skill_feedback import SkillFeedback
from memory.curator import SkillCuratorManager
from memory.user_model import UserModel
from tools import TOOL_REGISTRY
from security.vault import Vault
from security.approval import ApprovalGate
from security.question import QuestionGate
from security.shield import Shield


@dataclass
class AgentConfig:
    role: str
    session_id: str
    user_id: str = "default"
    # Mode autopilot (CLAUDE.md §1, §17): agent berjalan TANPA manusia di depan.
    # Tool yang butuh approval TIDAK dieksekusi — diantri sebagai proposal pending
    # ke approval_log untuk ditinjau user nanti. Default False (sesi interaktif biasa).
    autopilot: bool = False


@dataclass
class AgentEvent:
    """Event yang di-stream ke UI.

    `type="token"` → potongan jawaban (content) yang harus ditampilkan.
    `type="thinking"` → potongan reasoning model (bila ada): <think> lokal,
    extended-thinking Anthropic, atau parts.thought Gemini. Ditampilkan di blok
    collapsible terpisah, TIDAK masuk jawaban final.
    `type="status"` → sinyal proses (routing/thinking/tool/approval/fallback)
    agar user tahu agent sedang apa, bukan diam karena macet. `text` adalah
    label singkat untuk status, `detail` opsional (mis. nama model/tool).
    `type="usage"` → ringkasan biaya turn (tokens/cost/latency) di akhir run,
    dipakai conversation untuk mengagregasi total lintas-giliran.
    """

    type: str
    text: str = ""
    detail: str = ""
    usage: dict | None = None


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


def _format_tool_params(tool_name: str, params: dict) -> str:
    """Buat label ringkas 'tool_name(param=value)' untuk action chip di UI."""
    KEY_MAP = {
        "list_dir": "path",
        "file_read": "path",
        "file_write": "path",
        "shell_run": "command",
        "web_fetch": "url",
        "code_run": "code",
        "ask_user": "question",
    }
    key = KEY_MAP.get(tool_name)
    if key and key in params:
        val = str(params[key])
        if len(val) > 60:
            val = "…" + val[-57:]
        return f"{tool_name}({val})"
    return tool_name


def _validate_tool_input(tool, input_data: dict) -> str | None:
    """Validasi ringan input vs `input_schema` tool: required fields ada & tipe dasar cocok.

    Mengembalikan pesan error (untuk dikirim balik ke model agar memperbaiki) atau None
    bila valid. Sengaja minimal — bukan validator JSON-Schema penuh; cukup menangkap
    kesalahan umum model lokal (field hilang/null) tanpa dependency baru.
    """
    try:
        schema = tool.schema().get("input_schema", {})
    except Exception:  # noqa: BLE001 — schema rusak tak boleh menjatuhkan eksekusi
        return None
    if not isinstance(input_data, dict):
        return f"Input untuk '{tool.name}' harus objek, bukan {type(input_data).__name__}"
    required = schema.get("required", [])
    missing = [f for f in required if input_data.get(f) in (None, "")]
    if missing:
        return f"Tool '{tool.name}' butuh field: {', '.join(missing)}"
    return None


class AgentLoop:
    def __init__(
        self,
        agent_cfg: AgentConfig,
        db: DatabaseManager,
        config: AppConfig = CONFIG,
        approval: ApprovalGate | None = None,
        question_gate: QuestionGate | None = None,
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
        # Loop tertutup #1: offset threshold hasil kalibrasi dibaca dari DB tiap turn
        # (async), lalu di-set ke router sebelum decide(). Default 0 = router asli.
        self.calibration = CalibrationStore(db)
        # Override peta tier→model dari /router (dibaca per-turn, di-set sebelum decide()).
        self.router_config = RouterConfigStore(db)
        # Telemetri tool: dicatat di _execute_tool (titik eksekusi terpusat).
        self.tool_audit = ToolAudit(db)
        self.compactor = ContextCompactor(config.max_context_tokens)
        self.crystallizer = ConfidenceCrystallizer(agent_cfg.role, self.llm, db)
        # Compounding (I2/I3): jembatan outcome skill antar-turn (revive + promote + refine).
        self.skill_feedback = SkillFeedback(
            agent_cfg.role, db, self.decay, self.crystallizer, config
        )
        # Compounding (I1): konsolidasi skill mirip (throttled post-turn).
        self.curator = SkillCuratorManager(agent_cfg.role, db, self.llm, config)
        # Compounding (I5, opsional): profil user naratif (default nonaktif).
        self.user_model = UserModel(agent_cfg.role, db, self.llm, config)
        # ApprovalGate & QuestionGate di-inject dari Web UI agar resolve() mengenai
        # Future yang sama (AgentLoop dibuat baru per request, tapi gate harus shared).
        self.approval = approval or ApprovalGate(db, config)
        self.question_gate = question_gate or QuestionGate(config)
        self.shield = Shield()
        self.settings = SettingsStore(db)
        self.history: list[Turn] = []

        # nit #2: cache soul.toml sekali, jangan baca tiap turn
        self._soul = self._load_soul_once()

    def _load_soul_once(self) -> dict:
        with open(f"roles/{self.cfg.role}/soul.toml", "rb") as f:
            return tomllib.load(f)

    async def _maybe_compact(self, memory_ctx: dict, user_message: str) -> list[Turn]:
        """Pre-pass compaction headroom (opt-in /settings). Kembalikan history untuk build().

        Mode 'off' → history apa adanya (build() lalu truncation seperti biasa). Mode
        'local'/'cloud' → ringkas turn lama via summarizer bila melebihi budget. Semua
        jalur fail-safe ke history asli (§1.3 kegagalan anggun) — tak pernah jatuhkan turn.
        """
        mode = await self.settings.get_compaction_mode(self.config.compaction_default_mode)
        if mode == "off" or len(self.history) <= self.config.compaction_keep_recent:
            return self.history

        async def _summarize(joined: str) -> str:
            prompt = (
                "Ringkas percakapan agent berikut menjadi catatan padat yang menyimpan "
                "fakta, keputusan, dan konteks penting untuk melanjutkan. Jangan menambah "
                "informasi baru. Maksimal beberapa kalimat.\n\n" + joined[:8000]
            )
            if mode == "local":
                prov, mdl = self.config.compaction_local_model
            else:  # cloud → lewat fallback chain (provider utama = item pertama chain)
                prov, mdl = self.config.fallback_chain[-1]
            text = ""
            async for chunk in self.llm.stream_with_fallback(
                prov, mdl, [{"role": "user", "content": prompt}]
            ):
                if chunk.type == "text":
                    text += chunk.text
            return text

        # Sisakan ruang untuk system prompt + user message agar peringkasan dipicu
        # sebelum truncation menendang turn (estimasi kasar, konsisten build()).
        reserve = self.compactor.estimate_context_tokens(
            [
                {"role": "system", "content": self._soul["system_prompt"]["content"]},
                {"role": "user", "content": user_message},
            ]
        )
        try:
            return await self.compactor.compact(
                self.history,
                _summarize,
                keep_recent=self.config.compaction_keep_recent,
                min_old_turns=self.config.compaction_min_old_turns,
                reserve_tokens=reserve,
            )
        except Exception as e:  # noqa: BLE001 — compaction gagal → truncation aman
            log.warning("compaction_failed", session=self.cfg.session_id, error=str(e))
            return self.history

    async def run(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        start = time.monotonic()

        # 0. Shield: scan input (lapisan kosmetik, BUKAN pertahanan utama — lihat §17).
        # Pertahanan utama tetap container isolation di code_run.
        safe, reason = self.shield.scan_input(user_message)
        if not safe:
            log.warning("shield_blocked_input", session=self.cfg.session_id, reason=reason)
            yield AgentEvent(type="token", text=reason)
            return

        # 1. Deteksi koreksi user (audit feedback) [#1]
        # Audit: JANGAN gate dengan self.history — AgentLoop dibuat baru tiap request
        # web (history selalu kosong di awal), sehingga koreksi tak pernah terdeteksi.
        # check_correction aman dipanggil selalu: hanya UPDATE bila ada event sebelumnya
        # untuk session ini (turn sebelumnya), berdasarkan session_id yang persisten.
        corrected = await self.auditor.check_correction(user_message, self.cfg.session_id)

        # 1b. Compounding (I2/I3): resolusi outcome skill turn SEBELUMNYA berdasarkan
        # apakah turn ini mengoreksinya. Sukses → revive/promote; dikoreksi → reset/refine.
        # Dijalankan di awal turn (sinyal koreksi baru diketahui sekarang).
        await self.skill_feedback.resolve_previous(
            self.cfg.session_id, corrected, correction_trace=user_message if corrected else ""
        )

        # 2. Load skill aktif (belum decayed) [#2]
        active_skills = await self.decay.get_active_skills(query=user_message)

        # 3. Memory context (+ profil user I5 bila diaktifkan)
        memory_ctx = await self.memory.load_context(user_message, active_skills)
        profile = await self.user_model.get_active_profile()
        if profile:
            memory_ctx["user_model"] = profile

        # 4. Compaction headroom (opt-in /settings, default OFF): bila diaktifkan &
        # history melebihi budget, ringkas turn lama jadi satu blok alih-alih dibuang
        # (truncation). Pre-pass async sebelum build() sinkron tetap utuh. Fail-safe:
        # mode 'off' / error / ringkasan kosong → history apa adanya (truncation lama).
        history_for_build = await self._maybe_compact(memory_ctx, user_message)

        # 4b. Build messages
        messages = self.compactor.build(
            soul=self._soul["system_prompt"]["content"],
            memory=memory_ctx,
            history=history_for_build,
            user_message=user_message,
        )
        # Token-first (§1.4): ukur context window terpakai untuk meter budget di UI.
        context_tokens = self.compactor.estimate_context_tokens(messages)

        # 5. Route (soul-aware) + log [#1]
        # Terapkan offset kalibrasi + peta model terbaru sebelum memutuskan.
        self.router.threshold_offset = await self.calibration.get_offset()
        self.router.model_map = await self.router_config.get_map()
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
        # Status: beri tahu UI model/provider yang dipilih sebelum LLM dipanggil.
        yield AgentEvent(type="status", text="routing", detail=f"{route.provider}:{route.model}")

        # 6. Iterative tool loop (audit #10: bukan rekursif)
        turn = Turn(role="assistant", model_used=route.model)
        tools_schema = self._tools_for_role()

        async for event in self._run_tool_loop(messages, route, tools_schema, turn):
            yield event

        # 7. Finalize
        turn.latency_ms = int((time.monotonic() - start) * 1000)
        turn.cost_usd = route.cost_per_1k * (turn.tokens_in + turn.tokens_out) / 1000
        self.history.append(Turn(role="user", content=user_message))
        self.history.append(turn)
        await self.auditor.finalize(event_id, turn)

        # Ringkasan biaya turn → UI (conversation mengagregasi lintas-giliran).
        # context_tokens + max → meter budget token-first (§1.4); peringatan saat
        # mendekati batas dirender frontend dari rasio ini.
        yield AgentEvent(
            type="usage",
            usage={
                "tokens_in": turn.tokens_in,
                "tokens_out": turn.tokens_out,
                "cost_usd": turn.cost_usd,
                "latency_ms": turn.latency_ms,
                "model": turn.model_used,
                "context_tokens": context_tokens,
                "max_context_tokens": self.config.max_context_tokens,
            },
        )

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
    ) -> AsyncGenerator[AgentEvent, None]:
        """Audit #10: iterative, bukan rekursif."""
        hop = 0
        # Deteksi loop: track (tool_name, input_repr) dari panggilan sebelumnya.
        # Jika model memanggil tool yang sama dengan input identik berturut-turut,
        # injeksi peringatan ke context agar model tidak stuck looping.
        last_call: tuple[str, str] | None = None
        repeat_count = 0

        while hop <= self.config.max_tool_hops:
            pending_tool = None
            # Status: LLM mulai memproses (mengisi gap antara request dan token pertama).
            yield AgentEvent(type="status", text="thinking")
            async for chunk in self.llm.stream_with_fallback(
                route.provider, route.model, messages, tools_schema
            ):
                if chunk.type == "text":
                    turn.content += chunk.text
                    yield AgentEvent(type="token", text=chunk.text)
                elif chunk.type == "thinking":
                    # Reasoning model → blok terpisah di UI. JANGAN masuk turn.content
                    # (itu jawaban final yang di-crystallize/diarsipkan, bukan nalar).
                    yield AgentEvent(type="thinking", text=chunk.text)
                elif chunk.type == "tool_call":
                    pending_tool = chunk
                elif chunk.type == "usage":
                    turn.tokens_in = chunk.usage.get("input_tokens", 0)
                    turn.tokens_out = chunk.usage.get("output_tokens", 0)
                elif chunk.type == "fallback" and chunk.fallback_used:
                    turn.fallback_used = True
                    yield AgentEvent(type="status", text="fallback", detail=route.model)

            if not pending_tool:
                break  # tidak ada tool call → selesai

            # Deteksi panggilan identik berturut-turut (tool + input sama persis).
            # Gemma lokal mengabaikan pesan peringatan di context, jadi hard-break
            # langsung tanpa memberi kesempatan model lagi.
            call_key = (pending_tool.tool_name, repr(sorted(pending_tool.tool_input.items())))
            if call_key == last_call:
                repeat_count += 1
                if repeat_count >= 2:
                    log.warning(
                        "tool_loop_detected",
                        tool=pending_tool.tool_name,
                        repeat=repeat_count + 1,
                        session=self.cfg.session_id,
                    )
                    yield AgentEvent(
                        type="status",
                        text="loop_stopped",
                        detail=pending_tool.tool_name,
                    )
                    break  # hard stop — model mengabaikan pesan peringatan
            else:
                repeat_count = 0
            last_call = call_key

            # ask_user menunggu input manusia → beri tahu UI agar memunculkan kotak
            # jawaban (detail = teks pertanyaan), bukan sekadar chip "tool".
            if pending_tool.tool_name == "ask_user":
                question = str(pending_tool.tool_input.get("question", "")).strip()
                yield AgentEvent(type="status", text="question", detail=question)
            else:
                # Status: tool akan dijalankan — tampilkan nama tool + parameter utamanya
                # agar user bisa lihat path/command apa yang sedang dijelajahi.
                param_preview = _format_tool_params(pending_tool.tool_name, pending_tool.tool_input)
                yield AgentEvent(type="status", text="tool", detail=param_preview)
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

        # ask_user: klarifikasi interaktif lewat QuestionGate (menggantikan stub lama).
        # Tool-nya tetap menyediakan schema, tapi eksekusi nyata menunggu jawaban user
        # via Future yang di-resolve Web UI — pola sama ApprovalGate.
        if name == "ask_user":
            question = str(input_data.get("question", "")).strip()
            if not question:
                return {"error": "ask_user butuh field 'question'"}
            answer = await self.question_gate.ask(self.cfg.session_id, question)
            return {"answer": answer}

        # Validasi input vs schema SEBELUM approval/eksekusi: model lokal sering
        # kirim argumen salah bentuk. Pesan jelas balik ke model agar ia memperbaiki,
        # bukan menunggu approval lalu gagal. (Tidak menjalankan tool → tanpa telemetri.)
        schema_err = _validate_tool_input(tool, input_data)
        if schema_err:
            return {"error": schema_err}

        # Tool internal per-sesi (todo_write, report_blocker): suntik konteks sesi/role.
        # Tool tak menerima ini via signature execute; model tak perlu — & tak boleh —
        # mengarang session_id/role (sumber kebenaran = AgentLoop, bukan output model).
        if name in ("todo_write", "report_blocker"):
            input_data = {
                **input_data,
                "_session_id": self.cfg.session_id,
                "_role": self.cfg.role,
            }

        if tool.requires_approval:
            # Autopilot (§1, §17): tidak ada manusia untuk approve → JANGAN eksekusi.
            # Antri sebagai proposal pending agar user meninjau nanti. Tanpa ini,
            # ApprovalGate.request() akan menggantung sampai timeout lalu DENY —
            # "rusak", bukan "aman". Di sini aman secara eksplisit: aksi destruktif
            # terjadwal jadi PROPOSAL, bukan eksekusi diam-diam.
            if self.cfg.autopilot:
                await self.approval.queue_proposal(self.cfg.session_id, name, input_data)
                return {
                    "proposed": True,
                    "note": (
                        f"Aksi '{name}' butuh persetujuan & tidak dijalankan otomatis di "
                        "autopilot. Diantri sebagai proposal untuk ditinjau user. "
                        "Lanjutkan tanpa hasil aksi ini."
                    ),
                }
            approved = await self.approval.request(self.cfg.session_id, name, input_data)
            if not approved:
                return {"error": f"Tool '{name}' ditolak oleh user"}

        # Jaring pengaman §1.3: timeout + tangkap SEMUA exception → error dict yang
        # anggun + telemetri. Satu tool yang menggantung/melempar tidak menjatuhkan turn.
        started = time.monotonic()
        outcome = "ok"
        try:
            result = await asyncio.wait_for(
                tool.execute(input_data, vault=self.vault, db=self.db),
                timeout=self.config.tool_timeout_sec,
            )
            result = self._truncate_tool_output(result)
        except asyncio.TimeoutError:
            outcome = "timeout"
            log.warning("tool_timeout", tool=name, session=self.cfg.session_id)
            result = {
                "error": f"Tool '{name}' melebihi batas waktu {self.config.tool_timeout_sec}s"
            }
        except Exception as exc:  # noqa: BLE001 — tool pihak ketiga, kegagalan harus anggun
            outcome = "error"
            log.error("tool_failed", tool=name, error=str(exc), session=self.cfg.session_id)
            result = {"error": f"Tool '{name}' gagal: {exc}"}
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self.tool_audit.record(
                self.cfg.session_id, self.cfg.role, name, outcome, latency_ms
            )
        return result

    def _truncate_tool_output(self, result: dict) -> dict:
        """Potong field teks panjang ke tool_max_output (token-first §1.4) secara seragam.

        Tiap tool sebelumnya memotong sendiri dengan batas berbeda; ini jaring akhir
        agar tidak ada satu tool pun yang membanjiri context window.
        """
        if not isinstance(result, dict):
            return result
        limit = self.config.tool_max_output
        out: dict = {}
        for k, v in result.items():
            if isinstance(v, str) and len(v) > limit:
                out[k] = v[:limit] + f"\n…[dipotong, {len(v) - limit} char lagi]"
            else:
                out[k] = v
        return out

    def _tool_allowed(self, name: str) -> bool:
        allowed = self._soul.get("tools", {}).get("allowed", [])
        if name in allowed:
            return True
        # Izin MCP via wildcard agar role tak perlu mendaftar tiap tool yang
        # di-discover dinamis: "mcp__*" (semua MCP) atau "mcp__<server>__*" (satu server).
        # Tetap OPT-IN eksplisit (§1) — tanpa wildcard di soul, MCP tool ditolak.
        if name.startswith("mcp__"):
            for pat in allowed:
                if pat == "mcp__*" or (pat.endswith("*") and name.startswith(pat[:-1])):
                    return True
        return False

    def _tools_for_role(self) -> list:
        """Nit #1: hanya kirim schema tool yang diizinkan ke LLM."""
        return [t.schema() for n, t in TOOL_REGISTRY.items() if self._tool_allowed(n)]

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

        # Compounding (I2/I3): catat skill yang DIPAKAI turn ini agar turn berikutnya
        # bisa menilai outcome-nya (sukses → revive/promote; dikoreksi → reset/refine).
        used_ids = [s["id"] for s in active_skills if s.get("id") is not None]
        await self.skill_feedback.record_usage(self.cfg.session_id, used_ids)

        # Compounding (I1): konsolidasi skill mirip — throttled (1×/hari), mayoritas no-op.
        await self.curator.maybe_run_curation_pass()
        # Compounding (I4): router menyetel diri dalam rem — opt-in, throttled, default no-op.
        await self.calibration.maybe_auto_apply(self.config)
        # Compounding (I5, opsional): perbarui profil user — default nonaktif, throttled.
        await self.user_model.maybe_update()

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
