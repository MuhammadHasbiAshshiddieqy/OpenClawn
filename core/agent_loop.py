import asyncio
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator

from infra.chat_sessions import ChatSessionStore, truncate_for_title_prompt
from infra.config import AppConfig, CONFIG
from infra.database import DatabaseManager
from infra.logging import log
from infra.settings import SettingsStore
from infra.workspace import CURRENT_WORKSPACE_ROOT, SessionWorkspaceStore
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
from security.guardrails import GuardrailEngine, RailStage
from core.guardrails_config import GuardrailConfigStore


@dataclass
class AgentConfig:
    role: str
    session_id: str
    user_id: str = "default"
    # Mode autopilot (CLAUDE.md §1, §17): agent berjalan TANPA manusia di depan.
    # Tool yang butuh approval TIDAK dieksekusi — diantri sebagai proposal pending
    # ke approval_log untuk ditinjau user nanti. Default False (sesi interaktif biasa).
    autopilot: bool = False
    # Working directory adaptif per-sesi (§ user request, ala Claude Code/OpenClaw):
    # folder pilihan user untuk SESI ini, menggantikan CONFIG.workspace_root global
    # hanya selama turn ini berjalan. None (default) → pakai CONFIG.workspace_root
    # (perilaku lama, tak ada perubahan). Divalidasi ada & directory di web/main.py
    # SEBELUM sampai sini (fail-closed: path tak valid tak pernah masuk ContextVar).
    workspace_override: str | None = None
    # Persist & muat ulang riwayat percakapan per-sesi dari DB (session_turns).
    # True (default) untuk single-agent chat: request web berikutnya (AgentLoop baru)
    # memuat kembali turn sebelumnya → agent ingat konteks (§ user report). False untuk
    # multi-agent: strategy sudah membangun transkrip sendiri di turn_input, memuat DB
    # akan menduplikasi & mencampur giliran antar-role dalam satu session_id.
    persist_history: bool = True
    # Trust mode per-sesi (§ user request otonomi): manusia SEDANG hadir di chat aktif
    # (beda dari autopilot — tanpa manusia sama sekali) dan memilih melewati klik
    # Approve untuk tool yang membutuhkannya. Tool TETAP dieksekusi sungguhan (via
    # ApprovalGate.auto_approve, bukan queue_proposal), hanya tercatat berbeda di
    # audit (decision="auto:trust_mode"). `_TRUST_MODE_EXEMPT` (di bawah) tak pernah
    # bisa dilewati toggle ini — code_run tetap SELALU approval (CLAUDE.md §1, aturan
    # non-negotiable, tidak disentuh oleh fitur ini). Default False (perilaku lama).
    trust_mode: bool = False


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
    `type="file_created"` → tool penulis file (`_FILE_WRITE_TOOLS`) sukses;
    `text` = path file (dalam workspace) agar UI menampilkan link download
    (`GET /workspace/download?path=...`).
    `type="status", text="approval"` → tool butuh persetujuan manusia SEDANG
    menunggu; `approval_id` dipakai UI untuk kirim `POST /approve` (Approve/Reject)
    tanpa harus menunggu `approval_timeout_sec` (dulu: tak ada cara approve dari UI
    chat, semua tool butuh-approval selalu timeout).
    """

    type: str
    text: str = ""
    detail: str = ""
    usage: dict | None = None
    approval_id: str | None = None


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


# Tool yang menulis/menimpa file di workspace — sukses dari salah satu ini memicu
# AgentEvent(type="file_created") agar UI bisa menawarkan link download.
_FILE_WRITE_TOOLS = frozenset(
    {"file_write", "file_edit", "file_append", "apply_patch", "doc_write", "pdf_write"}
)

# Tool yang TIDAK PERNAH boleh dilewati oleh AgentConfig.trust_mode — approval-nya
# adalah aturan keras CLAUDE.md §1 ("code_run → True selalu"), bukan preferensi tool
# yang bisa dilonggarkan fitur otonomi. Toggle trust mode di UI tetap memblokir ini
# ke jalur ApprovalGate.request() normal (menunggu klik manusia), sama seperti mode biasa.
_TRUST_MODE_EXEMPT = frozenset({"code_run"})


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


def _format_tool_result(tool_name: str, result: dict) -> str:
    """Ubah hasil tool jadi teks yang JELAS untuk model, bukan repr dict Python.

    Model lokal kecil (Gemma, DeepSeek) sering tak mengenali `{'ok': True, ...}`
    sebagai "sukses, selesai" lalu mengulang panggilan (§ user report: menulis file
    berulang). Kalimat eksplisit sukses/gagal + instruksi "jangan ulangi" jauh lebih
    mudah dipatuhi. Fallback ke str(result) untuk bentuk tak terduga.
    """
    if not isinstance(result, dict):
        return str(result)
    if result.get("error"):
        return f"ERROR: {result['error']}"
    if tool_name in _FILE_WRITE_TOOLS and result.get("ok") and result.get("path"):
        # Sinyal terminal yang tegas: file sudah ditulis, JANGAN tulis ulang.
        return (
            f"SUCCESS: file written to {result['path']} "
            f"({result.get('bytes', result.get('appended', result.get('replacements', 0)))} bytes). "
            "The file is now saved. Do NOT write it again — report completion to the user."
        )
    if result.get("ok"):
        return "SUCCESS: " + ", ".join(f"{k}={v}" for k, v in result.items() if k != "ok")
    return str(result)


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
        # Guardrails (ala NeMo): config on/off per rail dari app_settings; engine
        # dibangun per-turn agar perubahan UI langsung berlaku tanpa restart.
        self.guardrails_config = GuardrailConfigStore(db)
        # Metadata sidebar riwayat chat (§ user report: chat selalu ke-reset, tak
        # ada cara buka chat baru/lanjutkan/hapus riwayat). Hanya relevan single-agent
        # (persist_history) — lihat _post_turn untuk generate judul.
        self.chat_sessions = ChatSessionStore(db)
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
        # Working directory adaptif (§ user request): kalau user mengisi folder
        # kerja untuk sesi ini, tool file/shell/git memakainya lewat ContextVar
        # (bukan CONFIG.workspace_root global) untuk SELURUH turn ini. Token
        # di-reset di finally agar tak "bocor" ke request lain yang berbagi loop
        # event yang sama (contextvars per-Task, tapi reset eksplisit tetap lebih aman
        # daripada mengandalkan garbage collection Task).
        #
        # Prioritas: (1) form UI diisi eksplisit request ini → menang (user sadar
        # mengetik folder baru); (2) kalau kosong, folder yang agent SENDIRI set
        # lewat tool set_workdir di turn sebelumnya (§ user request "pindah
        # direktori dinamis lewat chat", persist di session_workspace — AgentLoop
        # baru tiap request, jadi harus dimuat balik dari DB); (3) default global.
        effective_override = self.cfg.workspace_override
        if not effective_override and self.cfg.persist_history:
            effective_override = await SessionWorkspaceStore(self.db).get(self.cfg.session_id)

        ws_token = None
        if effective_override:
            ws_token = CURRENT_WORKSPACE_ROOT.set(effective_override)
        try:
            async for ev in self._run(user_message):
                yield ev
        finally:
            if ws_token is not None:
                CURRENT_WORKSPACE_ROOT.reset(ws_token)

    async def _run(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        start = time.monotonic()

        # 0. Guardrails — INPUT rails (ala NeMo). Lapisan kosmetik, BUKAN pertahanan
        # utama (container isolation tetap utama, §17). Engine dibangun per-turn dari
        # config app_settings agar perubahan on/off di UI langsung berlaku.
        guardrails = GuardrailEngine(enabled=await self.guardrails_config.get_enabled())
        in_outcome = guardrails.check_input(user_message)
        if in_outcome.blocked:
            log.warning(
                "guardrail_blocked_input",
                session=self.cfg.session_id,
                reason=in_outcome.block_reason,
            )
            yield AgentEvent(type="token", text=in_outcome.block_reason)
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

        # 2b. Muat riwayat percakapan SESI INI dari DB ke self.history (§ user report:
        # agent seolah tak pernah baca chat sebelumnya, bahkan di sesi yang sama).
        # AgentLoop dibuat baru tiap request web → self.history kosong; tanpa ini
        # build() hanya melihat system + pesan baru, jadi "Mana file-nya?" tak punya
        # rujukan. Muat hanya bila history di-memori masih kosong (hindari duplikasi
        # bila loop yang sama dipakai ulang untuk >1 turn, mis. di test/CLI).
        if self.cfg.persist_history and not self.history:
            past = await self.memory.load_turns(limit=self.config.session_history_turns)
            self.history = [Turn(role=t["role"], content=t["content"]) for t in past]

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

        # 6b. Guardrails — OUTPUT rails (ala NeMo). Gap terbesar OpenCLAWN sebelumnya:
        # tak ada yang memeriksa respons LLM. Catatan jujur: token sudah di-stream ke
        # UI (tak bisa ditarik), jadi rail bekerja pada turn.content LENGKAP — meredaksi
        # PII / memblokir kebocoran SEBELUM disimpan ke history & memori, lalu memberi
        # tahu UI agar bisa menandai/menimpa. Deteksi + redaksi-penyimpanan tetap bernilai.
        guardrail_status = "clean"
        guardrail_detail = ""
        if turn.content:
            out_outcome = guardrails.run(RailStage.OUTPUT, turn.content)
            if out_outcome.blocked:
                log.warning(
                    "guardrail_blocked_output",
                    session=self.cfg.session_id,
                    reason=out_outcome.block_reason,
                )
                turn.content = out_outcome.text  # pesan tahanan, jangan simpan teks asli
                guardrail_status, guardrail_detail = "blocked", out_outcome.block_reason
                yield AgentEvent(type="guardrail", text="blocked", detail=out_outcome.block_reason)
            elif out_outcome.modified:
                redactions = [f for r in out_outcome.results for f in r.findings]
                log.info(
                    "guardrail_redacted_output",
                    session=self.cfg.session_id,
                    findings=redactions,
                )
                turn.content = out_outcome.text  # versi teredaksi disimpan & ditandai
                guardrail_status, guardrail_detail = "redacted", ", ".join(redactions)
                yield AgentEvent(type="guardrail", text="redacted", detail=", ".join(redactions))

        # 7. Finalize
        turn.latency_ms = int((time.monotonic() - start) * 1000)
        turn.cost_usd = route.cost_per_1k * (turn.tokens_in + turn.tokens_out) / 1000
        self.history.append(Turn(role="user", content=user_message))
        self.history.append(turn)
        # Persist giliran sesi INI ke DB agar request BERIKUTNYA (AgentLoop baru)
        # dapat memuatnya kembali (§ user report: konteks percakapan hilang). Simpan
        # setelah guardrail OUTPUT agar transkrip = versi teredaksi, bukan teks asli.
        # Hanya single-agent (persist_history); multi-agent kelola transkrip sendiri.
        if self.cfg.persist_history:
            await self.memory.append_turn("user", user_message)
            await self.memory.append_turn("assistant", turn.content)

        # Evidence-Based Response (TODO.md § Prioritas 2): snapshot policy/skill/
        # guardrail yang BENAR-BENAR berlaku turn ini, bukan cuma tersirat lintas
        # kolom terpisah. Confidence SENGAJA tidak disertakan di sini — crystallizer
        # jalan async di post_turn (bukan sinkron per-turn, hanya saat ≥3 tool call
        # & kondisi tertentu), jadi menyertakan confidence palsu/kosong di sini akan
        # menyesatkan. Query lewat GET /evidence/{event_id}.
        evidence = {
            "policy": {
                "provider": route.provider,
                "model": route.model,
                "complexity": route.complexity.value,
                "reason": route.reason,
            },
            "memory": [s.get("skill_name", "") for s in active_skills if s.get("skill_name")],
            "guardrail": {"status": guardrail_status, "detail": guardrail_detail},
        }
        await self.auditor.finalize(event_id, turn, evidence=evidence)

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
        # Deteksi loop KEDUA, lebih longgar (§ user report: approval berkali-kali
        # untuk task simpel): model kadang menulis ULANG file yang SAMA dengan isi
        # SEDIKIT beda tiap kali (whitespace/newline) — deteksi input-identik di atas
        # tak pernah kena karena call_key selalu "beda". Untuk tool penulis file,
        # tool+path yang sama berturut-turut ≥3× dalam SATU turn sudah cukup
        # mencurigakan (menulis file yang sama berulang bukan alur kerja normal).
        last_write_target: tuple[str, str] | None = None
        write_repeat_count = 0

        # Cap output lebih longgar saat tool tersedia (§ user report: "No answer"
        # — model reasoning-heavy kehabisan giliran SAAT MASIH merencanakan tool
        # mana yang dipakai, sebelum sempat bertindak/menjawab, dengan cap lama).
        # Turn tanpa tool tetap pakai default lama (llm_max_tokens_default).
        hop_max_tokens = (
            self.config.llm_max_tokens_with_tools
            if tools_schema
            else self.config.llm_max_tokens_default
        )

        while hop <= self.config.max_tool_hops:
            pending_tool = None
            # Status: LLM mulai memproses (mengisi gap antara request dan token pertama).
            yield AgentEvent(type="status", text="thinking")
            async for chunk in self.llm.stream_with_fallback(
                route.provider, route.model, messages, tools_schema, hop_max_tokens
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

            # Deteksi loop kedua (path sama, konten boleh beda) — hanya untuk tool
            # penulis file, karena menulis file BERBEDA berulang (mis. banyak file
            # dalam satu turn) adalah pola normal & tak boleh kena ini.
            if pending_tool.tool_name in _FILE_WRITE_TOOLS:
                write_target = (
                    pending_tool.tool_name,
                    str(pending_tool.tool_input.get("path", "")),
                )
                if write_target == last_write_target and write_target[1]:
                    write_repeat_count += 1
                    # Menulis path yang SAMA dua kali berturut-turut sudah cukup
                    # mencurigakan (menulis ulang file identik bukan alur kerja normal).
                    # Sebelumnya ≥3 — terlalu longgar, "hello world" pun ditulis 4×.
                    if write_repeat_count >= 1:
                        log.warning(
                            "tool_loop_detected_same_path",
                            tool=pending_tool.tool_name,
                            path=write_target[1],
                            repeat=write_repeat_count + 1,
                            session=self.cfg.session_id,
                        )
                        yield AgentEvent(
                            type="status",
                            text="loop_stopped",
                            detail=pending_tool.tool_name,
                        )
                        break  # hard stop — menulis path yang sama berulang kali
                else:
                    write_repeat_count = 0
                last_write_target = write_target

            # Trust mode (§ user request otonomi): sesi ini melewati approval manual
            # untuk tool yang mengizinkannya — TAPI tidak untuk _TRUST_MODE_EXEMPT
            # (code_run, CLAUDE.md §1 non-negotiable). Dihitung di sini (bukan di
            # _execute_tool saja) agar UI menampilkan chip "tool" biasa, bukan kartu
            # approval yang menunggu klik yang tak akan pernah terjadi.
            tool_obj = TOOL_REGISTRY.get(pending_tool.tool_name)
            bypass_approval = (
                self.cfg.trust_mode
                and not self.cfg.autopilot
                and pending_tool.tool_name not in _TRUST_MODE_EXEMPT
            )

            # ask_user menunggu input manusia → beri tahu UI agar memunculkan kotak
            # jawaban (detail = teks pertanyaan), bukan sekadar chip "tool".
            approval_id = None
            if pending_tool.tool_name == "ask_user":
                question = str(pending_tool.tool_input.get("question", "")).strip()
                yield AgentEvent(type="status", text="question", detail=question)
            elif tool_obj and tool_obj.requires_approval and bypass_approval:
                # Trust mode aktif: tool tetap dieksekusi (lewat auto_approve di
                # _execute_tool), tapi UI cukup lihat chip tool biasa + tanda "trust".
                param_preview = _format_tool_params(pending_tool.tool_name, pending_tool.tool_input)
                yield AgentEvent(type="status", text="tool_trusted", detail=param_preview)
            elif tool_obj and tool_obj.requires_approval and not self.cfg.autopilot:
                # Tool butuh approval manusia → pre-generate ID SEBELUM memanggil
                # _execute_tool (yang akan blocking menunggu Future) agar UI dapat
                # ID-nya lebih dulu dan bisa memasang tombol Approve/Reject sementara
                # request masih menunggu (dulu: UI tak tahu apa-apa sampai timeout).
                approval_id = uuid.uuid4().hex
                param_preview = _format_tool_params(pending_tool.tool_name, pending_tool.tool_input)
                yield AgentEvent(
                    type="status", text="approval", detail=param_preview, approval_id=approval_id
                )
            else:
                # Status: tool akan dijalankan — tampilkan nama tool + parameter utamanya
                # agar user bisa lihat path/command apa yang sedang dijelajahi.
                param_preview = _format_tool_params(pending_tool.tool_name, pending_tool.tool_input)
                yield AgentEvent(type="status", text="tool", detail=param_preview)
            result = await self._execute_tool(
                pending_tool.tool_name,
                pending_tool.tool_input,
                approval_id=approval_id,
                bypass_approval=bypass_approval,
            )
            turn.tool_calls.append(
                {"name": pending_tool.tool_name, "input": pending_tool.tool_input}
            )
            # Tool yang menulis file & berhasil → beri UI cara mengunduhnya (§ user
            # request: "file harusnya bisa di-download"). Hanya `ok=True` dengan
            # `path` yang dilaporkan tool itu sendiri (bukan input mentah model —
            # path bisa saja di-resolve/berubah oleh workspace guard).
            if (
                pending_tool.tool_name in _FILE_WRITE_TOOLS
                and isinstance(result, dict)
                and result.get("ok")
                and result.get("path")
            ):
                yield AgentEvent(type="file_created", text=str(result["path"]))
            # Tulis KEMBALI giliran tool ke messages: (1) assistant yang MEMANGGIL tool,
            # (2) hasil tool. Sebelumnya HANYA hasil yang di-append — model (terutama
            # Gemma/DeepSeek lokal) tak melihat rekaman bahwa IA sendiri sudah memanggil
            # tool, jadi ia memanggil ULANG tool yang sama (§ user report: "terus menerus
            # write file"). Dengan giliran assistant+tool_call di history, model tahu
            # aksi sudah dilakukan & hasilnya, lalu lanjut ke jawaban akhir. Format
            # tool_calls Ollama/OpenAI-compatible; provider lain mengabaikan field asing.
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": pending_tool.tool_name,
                                "arguments": pending_tool.tool_input,
                            }
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "name": pending_tool.tool_name,
                    "content": _format_tool_result(pending_tool.tool_name, result),
                }
            )
            hop += 1

    async def _execute_tool(
        self,
        name: str,
        input_data: dict,
        approval_id: str | None = None,
        bypass_approval: bool = False,
    ) -> dict:
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

        # Tool internal per-sesi (todo_write, report_blocker, set_workdir): suntik
        # konteks sesi/role. Tool tak menerima ini via signature execute; model tak
        # perlu — & tak boleh — mengarang session_id/role (sumber kebenaran =
        # AgentLoop, bukan output model).
        if name in ("todo_write", "report_blocker", "set_workdir"):
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
            # Trust mode (§ user request otonomi): caller (_run_tool_loop) sudah
            # menghitung bypass_approval dan MENGECUALIKAN _TRUST_MODE_EXEMPT
            # (code_run) — di sini hanya eksekusi keputusan itu, bukan mengevaluasi
            # ulang. Tool tetap benar-benar dijalankan (beda dari autopilot di atas).
            if bypass_approval and name not in _TRUST_MODE_EXEMPT:
                approved = await self.approval.auto_approve(self.cfg.session_id, name, input_data)
            else:
                approved = await self.approval.request(
                    self.cfg.session_id, name, input_data, approval_id=approval_id
                )
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
        # Sidebar riwayat chat (§ user report): tandai sesi aktif (urutan terbaru
        # dulu) + generate judul SEKALI di turn pertama. Hanya single-agent
        # (persist_history) — multi-agent tak punya entri sidebar sendiri.
        if self.cfg.persist_history:
            await self.chat_sessions.touch(self.cfg.session_id)
            if not await self.chat_sessions.has_title(self.cfg.session_id):
                await self._generate_session_title(user_message)

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

    async def _generate_session_title(self, user_message: str) -> None:
        """Judul sidebar dari pesan pertama sesi, via LLM lokal kecil (§ user request).

        `truncate_for_title_prompt` memotong pesan panjang jadi head+tail kata
        SEBELUM dikirim ke LLM — pesan pertama user bisa panjang, tak perlu
        membayar token generate judul untuk seluruh isinya (§ user request).
        Fail-safe (§1.3): LLM/parsing gagal → sesi tetap tanpa judul (sidebar
        fallback ke potongan mentah pesan), bukan menjatuhkan turn.
        """
        try:
            prov, mdl = self.config.compaction_local_model
            prompt = (
                "Buat judul chat SANGAT singkat (maksimal 6 kata, tanpa tanda kutip, "
                "tanpa titik di akhir) yang merangkum topik pesan berikut:\n\n"
                + truncate_for_title_prompt(user_message)
            )
            title = ""
            async for chunk in self.llm.stream_with_fallback(
                prov, mdl, [{"role": "user", "content": prompt}]
            ):
                if chunk.type == "text":
                    title += chunk.text
            title = title.strip()
            if title:
                await self.chat_sessions.set_title(self.cfg.session_id, title)
        except Exception as e:  # noqa: BLE001 — judul kosmetik, tak boleh jatuhkan turn
            log.warning(
                "session_title_generation_failed", session=self.cfg.session_id, error=str(e)
            )

    @staticmethod
    def _render_history(history: list[Turn]) -> str:
        """Serialisasi history jadi teks untuk arsip L4 (full_content yang bisa di-search)."""
        return "\n".join(f"{t.role}: {t.content}" for t in history if t.content)
