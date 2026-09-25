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
from infra.workspace import (
    CURRENT_WORKDIR_ROOTS,
    CURRENT_WORKSPACE_ROOT,
    SessionWorkspaceStore,
    validate_workdir_candidate,
)
from infra.sandbox_image import CURRENT_SANDBOX_IMAGE, SessionSandboxImageStore
from infra.sandbox_lifecycle import CURRENT_PERSISTENT_SANDBOX, SessionSandboxContainerStore
from tools.sandbox import DockerSandbox
from core.router import SmartRouter
from core.agent_identity import agent_identity
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
from tools.web import uses_vault_credential
from security.vault import Vault
from security.approval import ApprovalGate
from security.question import QuestionGate
from security.shield import Shield
from security.guardrails import GuardrailEngine, RailStage
from security.policy_engine import PolicyEngine
from core.guardrails_config import GuardrailConfigStore
from roles.registry import available_roles


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
    # Audit 2026-09-25 (#1): allowlist root folder kerja untuk turn ini. None →
    # warisi ContextVar yang sudah aktif (subtask Task Graph) atau default config
    # (`infra/workspace.py::default_workdir_roots`). web/main.py mengisi ini per
    # request dari RBAC — tuple kosong = user ini tak boleh pindah folder.
    workdir_roots: tuple[str, ...] | None = None
    # Audit 2026-09-25 (#11): access_role user pemilik turn (admin/member/viewer)
    # bila auth aktif; None = auth nonaktif / caller non-web. Dipakai tool yang
    # perlu RBAC (db_query admin-only di mode multi-user).
    access_role: str | None = None
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
    # § Task Graph (DAG subtask, core/task_executor.py): identitas node saat
    # AgentLoop ini menjalankan SATU subtask dalam sebuah graph, bukan turn
    # chat biasa. None (default) = turn biasa, tak ada perubahan perilaku.
    # Diteruskan ke RoutingAuditor/ApprovalGate/ToolAudit (kolom nullable,
    # pola sama agent_identity) agar audit trail subtask query-able per graph.
    task_id: str | None = None
    node_id: str | None = None


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
    # Audit 2026-09-25 (#5): model yang BENAR-BENAR menghasilkan tiap hop (setelah
    # fallback), bukan cuma pilihan router. Dipakai crystallizer agar evaluator
    # dipilih terhadap generator sebenarnya.
    models_used: list = field(default_factory=list)


def generator_model_of(turn: "Turn") -> str:
    """Generator yang dilaporkan ke crystallizer. Lebih dari satu model
    berkontribusi (fallback di tengah turn) → label gabungan yang SENGAJA tak ada
    di EVALUATOR_FOR, sehingga crystallizer menandainya unverified (draft) —
    tak ada cara menjamin satu evaluator setara dengan SEMUA generator itu."""
    distinct = list(dict.fromkeys(turn.models_used))
    if len(distinct) > 1:
        return "mixed:" + "+".join(distinct)
    return distinct[0] if distinct else turn.model_used


# Tool yang menulis/menimpa file di workspace — sukses dari salah satu ini memicu
# AgentEvent(type="file_created") agar UI bisa menawarkan link download.
_FILE_WRITE_TOOLS = frozenset(
    {"file_write", "file_edit", "file_append", "apply_patch", "doc_write", "pdf_write"}
)

# Tool yang TIDAK PERNAH boleh dilewati oleh AgentConfig.trust_mode — approval-nya
# adalah aturan keras CLAUDE.md §1 ("code_run → True selalu"), bukan preferensi tool
# yang bisa dilonggarkan fitur otonomi. Toggle trust mode di UI tetap memblokir ini
# ke jalur ApprovalGate.request() normal (menunggu klik manusia), sama seperti mode biasa.
# "build_sandbox_image" (§ Prioritas 8.3): sekelas sensitivitas dengan code_run —
# `docker build`-nya sendiri membuka network sementara (§ residual risk didokumentasikan
# `DockerSandbox.build_project_image`), jadi tak boleh lolos trust mode juga.
# "sandbox_persist_enable" (§ Fase 3, sandbox lifecycle): SEKALIGUS lebih sensitif —
# membuat state WRITABLE yang bertahan lintas panggilan (bukan cuma network
# sesaat saat build), jadi non-negotiable sama seperti dua tool di atas.
_TRUST_MODE_EXEMPT = frozenset({"code_run", "build_sandbox_image", "sandbox_persist_enable"})

# Audit 2026-09-25: referensi kuat ke task _post_turn yang sedang berjalan. Event
# loop hanya memegang weak reference ke Task (dokumentasi asyncio.create_task) —
# tanpa ini task post-turn (tulis memori, decay, crystallize) bisa di-GC di
# tengah jalan setelah request web selesai.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _trust_mode_exempt(name: str, tool_input: dict) -> bool:
    """True bila panggilan ini TAK boleh dilewati trust mode.

    Selain `_TRUST_MODE_EXEMPT`, audit 2026-09-25 (#3): http_request yang
    menyuntik credential `vault:KEY` ke header — tanpa ini trust mode membuat
    prompt injection bisa mengirim credential ke host mana pun tanpa satu klik."""
    if name in _TRUST_MODE_EXEMPT:
        return True
    return name == "http_request" and uses_vault_credential(tool_input)


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


def _soul_allows_tool(soul: dict, name: str) -> bool:
    """Cek `[tools] allowed` di satu soul.toml SUDAH DIMUAT (dict), tanpa perlu instance
    `AgentLoop`. Diekstrak dari `AgentLoop._tool_allowed` (yang delegasi ke sini dengan
    `self._soul`) agar `core/late_execute.py` (durable execution, TODO.md § Prioritas 8.1)
    bisa memvalidasi ulang izin role untuk approval yatim lintas restart tanpa
    menduplikasi logika keamanan ini di dua tempat."""
    allowed = soul.get("tools", {}).get("allowed", [])
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
        self.memory = MemoryManager(
            agent_cfg.role, agent_cfg.session_id, db, user_id=agent_cfg.user_id
        )
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
        # Non-Human Identity (TODO.md § Prioritas 9.2): "{role}@{hash12}" dari
        # SELURUH soul.toml efektif — dihitung sekali di sini (soul sudah
        # di-cache), sama pola dengan self._soul di atas. Config berubah →
        # AgentLoop instance BERIKUTNYA (soul.toml dibaca ulang) otomatis
        # dapat identitas baru; instance yang sedang berjalan tetap konsisten
        # dengan identitas yang dihitung di awal, bukan berubah di tengah turn.
        self.agent_identity = agent_identity(self.cfg.role, self._soul)
        # Policy Engine (TODO.md § Prioritas 3): lapisan kondisi TAMBAHAN di atas
        # allow-list [tools] dan Tool.requires_approval statis — dibaca dari
        # soul.toml [policy.<tool_name>]. Section opsional; role tanpa [policy]
        # sama sekali → semua tool ALLOW default (perilaku lama tak berubah).
        self.policy_engine = PolicyEngine(self._soul.get("policy", {}))

    def _load_soul_once(self) -> dict:
        # Audit produksi 2026-09-18 (kritis, privilege escalation): `role`
        # bisa datang MENTAH dari form field (`/chat/stream`, `/converse/stream`
        # participants) atau argumen tool (`task_graph_submit`) — TANPA
        # validasi apa pun sebelumnya, string traversal memuat soul.toml
        # ARBITRER dari luar roles/ (tool allow-list/system-prompt bikinan
        # penyerang). Lihat roles/registry.py::available_roles untuk detail
        # & reproduksi. Ini titik pertahanan UTAMA — melindungi SEMUA caller
        # AgentLoop, bukan cuma web/main.py.
        if self.cfg.role not in available_roles():
            raise ValueError(f"role tidak dikenal: '{self.cfg.role}'")
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
        roots_token = None
        if self.cfg.workdir_roots is not None:
            roots_token = CURRENT_WORKDIR_ROOTS.set(tuple(self.cfg.workdir_roots))

        effective_override = self.cfg.workspace_override
        if not effective_override and self.cfg.persist_history:
            effective_override = await SessionWorkspaceStore(self.db).get(self.cfg.session_id)
        # Audit 2026-09-25 (#1): validasi ULANG terhadap allowlist turn ini — nilai
        # tersimpan di session_workspace bisa berasal dari sebelum perbaikan (mis.
        # "/") atau dari user dengan hak berbeda. Tak lolos → abaikan (workspace
        # default), bukan dipakai diam-diam.
        if effective_override:
            validated, err = validate_workdir_candidate(effective_override)
            if err:
                log.warning(
                    "workdir_override_rejected",
                    session=self.cfg.session_id,
                    workdir=effective_override,
                    reason=err,
                )
            effective_override = validated

        ws_token = None
        if effective_override:
            ws_token = CURRENT_WORKSPACE_ROOT.set(effective_override)

        # Sandbox image proyek adaptif (§ Prioritas 8.3): sama pola & alasan
        # persis dengan folder kerja di atas — bila sesi ini pernah sukses
        # `build_sandbox_image`, code_run/shell_run untuk SISA sesi (termasuk
        # lintas restart server, dipulihkan dari session_sandbox_image) harus
        # otomatis memakai image itu, bukan diam-diam balik ke SANDBOX_IMAGE
        # dasar. Token di-reset di finally — sama alasan (jangan bocor ke
        # request lain yang berbagi loop event).
        sandbox_image = None
        if self.cfg.persist_history:
            sandbox_image = await SessionSandboxImageStore(self.db).get(self.cfg.session_id)
        img_token = None
        if sandbox_image:
            img_token = CURRENT_SANDBOX_IMAGE.set(sandbox_image)

        # Sandbox PERSISTEN (§ Fase 3, sandbox lifecycle — owner disetujui
        # eksplisit): sama pola & alasan persis dengan dua ContextVar di atas.
        # Bila sesi ini pernah sukses `sandbox_persist_enable`, code_run untuk
        # SISA sesi (termasuk lintas restart server — container Docker & baris
        # DB sama-sama bertahan lintas restart proses app) otomatis exec ke
        # container itu, bukan diam-diam balik ke ephemeral. `touch()` menandai
        # container ini BENAR-BENAR dipakai turn ini (dibaca `core/sandbox_reaper.py`
        # untuk keputusan idle/destroy) — dan bila sempat di-pause karena idle,
        # bangunkan otomatis DI SINI, transparan bagi model (tak perlu tool baru
        # untuk "resume", `code_run` biasa saja cukup).
        # Audit produksi 2026-09-15: seluruh blok ini dibungkus try/except — Docker
        # yang sepenuhnya tak tersedia (binary hilang/daemon berhenti) membuat
        # `resume_persistent` RAISE `SandboxUnavailable` (bukan return dict error),
        # dan itu terjadi SEBELUM `try/finally` di bawah mulai. Tanpa guard ini,
        # SETIAP turn berikutnya untuk sesi itu crash total sebelum LLM sempat
        # dipanggil sama sekali (tak ada routing/audit/jawaban) — DAN kondisi
        # `state='paused'` di DB tak pernah berubah, jadi sesi itu rusak PERMANEN
        # sampai operator turun tangan manual. Melanggar CLAUDE.md §1.3 ("setiap
        # dependency eksternal punya kegagalan yang anggun") — dependency Docker
        # di sini diperlakukan berbeda dari di titik lain (mis. `_execute_tool`
        # sudah menangkap SEMUA exception tool). Fail-safe: log lalu lanjut TANPA
        # persist_token (turn ini jatuh ke jalur ephemeral seperti sesi yang tak
        # pernah opt-in — `code_run` tetap bisa gagal anggun lewat `_execute_tool`
        # seperti biasa, bukan menjatuhkan seluruh turn).
        persist_token = None
        if self.cfg.persist_history:
            try:
                container = await SessionSandboxContainerStore(self.db).get(self.cfg.session_id)
                if container is not None:
                    await SessionSandboxContainerStore(self.db).touch(self.cfg.session_id)
                    if container["state"] == "paused":
                        sandbox = DockerSandbox()
                        result = await sandbox.resume_persistent(container["container_id"])
                        if result["ok"]:
                            await SessionSandboxContainerStore(self.db).set_state(
                                self.cfg.session_id, "running"
                            )
                        else:
                            log.warning(
                                "sandbox_persistent_resume_failed",
                                session=self.cfg.session_id,
                                error=result["error"],
                            )
                    persist_token = CURRENT_PERSISTENT_SANDBOX.set(container["container_id"])
            except Exception as e:  # noqa: BLE001 — Docker mati tak boleh jatuhkan seluruh turn
                log.warning(
                    "sandbox_persistent_restore_failed",
                    session=self.cfg.session_id,
                    error=str(e),
                )
        try:
            async for ev in self._run(user_message):
                yield ev
        finally:
            if ws_token is not None:
                CURRENT_WORKSPACE_ROOT.reset(ws_token)
            if img_token is not None:
                CURRENT_SANDBOX_IMAGE.reset(img_token)
            if persist_token is not None:
                CURRENT_PERSISTENT_SANDBOX.reset(persist_token)
            if roots_token is not None:
                CURRENT_WORKDIR_ROOTS.reset(roots_token)

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
        # Audit 2026-09-25 (#6): pembelajaran skill bersifat opsional — gagal di
        # sini (DB/LLM) tak boleh menggagalkan jawaban untuk user.
        try:
            await self.skill_feedback.resolve_previous(
                self.cfg.session_id, corrected, correction_trace=user_message if corrected else ""
            )
        except Exception as e:  # noqa: BLE001 — lihat komentar di atas
            log.warning("skill_feedback_resolve_failed", session=self.cfg.session_id, error=str(e))

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
            self.cfg.session_id,
            self.cfg.role,
            user_message,
            route,
            user_id=self.cfg.user_id,
            agent_identity=self.agent_identity,
            task_id=self.cfg.task_id,
            node_id=self.cfg.node_id,
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
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
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
            # Audit 2026-09-25: SEMUA tool call dalam satu hop dieksekusi (dulu
            # hanya yang TERAKHIR — panggilan paralel dari Claude/Gemini dibuang
            # diam-diam, dan Anthropic menolak giliran berikut karena tool_use
            # tanpa pasangan tool_result).
            pending_tools: list = []
            hop_model = route.model
            hop_text = ""
            hop_usage: dict = {}
            # Status: LLM mulai memproses (mengisi gap antara request dan token pertama).
            yield AgentEvent(type="status", text="thinking")
            async for chunk in self.llm.stream_with_fallback(
                route.provider, route.model, messages, tools_schema, hop_max_tokens
            ):
                if chunk.type == "text":
                    turn.content += chunk.text
                    hop_text += chunk.text
                    yield AgentEvent(type="token", text=chunk.text)
                elif chunk.type == "thinking":
                    # Reasoning model → blok terpisah di UI. JANGAN masuk turn.content
                    # (itu jawaban final yang di-crystallize/diarsipkan, bukan nalar).
                    yield AgentEvent(type="thinking", text=chunk.text)
                elif chunk.type == "tool_call":
                    pending_tools.append(chunk)
                elif chunk.type == "usage":
                    # Dalam satu hop: nilai TERAKHIR (Gemini mengirim usage kumulatif
                    # di tiap chunk). Antar hop: DIJUMLAH (audit 2026-09-25) — tiap
                    # hop panggilan LLM terpisah yang ditagih penuh; sebelumnya hop
                    # terakhir menimpa hop sebelumnya, biaya turn bertool terlalu rendah.
                    hop_usage = chunk.usage
                elif chunk.type == "fallback" and chunk.fallback_used:
                    turn.fallback_used = True
                    hop_model = chunk.fallback_model or hop_model
                    yield AgentEvent(type="status", text="fallback", detail=hop_model)
            turn.tokens_in += hop_usage.get("input_tokens", 0) or 0
            turn.tokens_out += hop_usage.get("output_tokens", 0) or 0
            turn.model_used = hop_model
            turn.models_used.append(hop_model)

            if not pending_tools:
                break  # tidak ada tool call → selesai

            executed: list[tuple] = []  # (chunk, call_id, result)
            stop = False
            for pending_tool in pending_tools:
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
                        stop = True
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
                            stop = True
                            break  # hard stop — menulis path yang sama berulang kali
                    else:
                        write_repeat_count = 0
                    last_write_target = write_target

                # Policy Engine (TODO.md § Prioritas 3): dievaluasi DI SINI (bukan hanya
                # di _execute_tool) agar status UI & keputusan trust-mode-bypass di bawah
                # konsisten dengan apa yang benar-benar terjadi saat eksekusi — tanpa ini,
                # tool yang defaultnya requires_approval=False (mis. shell_run) tapi
                # dipaksa approval oleh policy akan salah tampil sebagai chip "tool" biasa.
                tool_obj = TOOL_REGISTRY.get(pending_tool.tool_name)
                policy_decision = self.policy_engine.evaluate(
                    pending_tool.tool_name, pending_tool.tool_input
                )
                policy_forces_approval = policy_decision.action == "require_approval"
                requires_approval_effective = bool(tool_obj and tool_obj.requires_approval) or (
                    policy_forces_approval
                )

                # Trust mode (§ user request otonomi): sesi ini melewati approval manual
                # untuk tool yang mengizinkannya — TAPI tidak untuk _TRUST_MODE_EXEMPT
                # (code_run, CLAUDE.md §1 non-negotiable) DAN TIDAK untuk approval yang
                # DIPAKSA Policy Engine (§ keputusan desain: policy adalah lapisan
                # keamanan yang lebih kuat daripada preferensi otonomi sesi — kalau
                # trust mode bisa melewatinya, policy jadi tidak berarti apa-apa saat
                # trust mode aktif). Dihitung di sini (bukan di _execute_tool saja) agar
                # UI menampilkan chip "tool" biasa, bukan kartu approval yang menunggu
                # klik yang tak akan pernah terjadi.
                bypass_approval = (
                    self.cfg.trust_mode
                    and not self.cfg.autopilot
                    and not _trust_mode_exempt(pending_tool.tool_name, pending_tool.tool_input)
                    and not policy_forces_approval
                )

                # ask_user menunggu input manusia → beri tahu UI agar memunculkan kotak
                # jawaban (detail = teks pertanyaan), bukan sekadar chip "tool".
                approval_id = None
                if pending_tool.tool_name == "ask_user":
                    question = str(pending_tool.tool_input.get("question", "")).strip()
                    yield AgentEvent(type="status", text="question", detail=question)
                elif requires_approval_effective and bypass_approval:
                    # Trust mode aktif: tool tetap dieksekusi (lewat auto_approve di
                    # _execute_tool), tapi UI cukup lihat chip tool biasa + tanda "trust".
                    param_preview = _format_tool_params(
                        pending_tool.tool_name, pending_tool.tool_input
                    )
                    yield AgentEvent(type="status", text="tool_trusted", detail=param_preview)
                elif requires_approval_effective and not self.cfg.autopilot:
                    # Tool butuh approval manusia (statis ATAU dipaksa policy) → pre-generate
                    # ID SEBELUM memanggil _execute_tool (yang akan blocking menunggu Future)
                    # agar UI dapat ID-nya lebih dulu dan bisa memasang tombol Approve/Reject
                    # sementara request masih menunggu (dulu: UI tak tahu apa-apa sampai timeout).
                    approval_id = uuid.uuid4().hex
                    param_preview = _format_tool_params(
                        pending_tool.tool_name, pending_tool.tool_input
                    )
                    yield AgentEvent(
                        type="status",
                        text="approval",
                        detail=param_preview,
                        approval_id=approval_id,
                    )
                else:
                    # Status: tool akan dijalankan — tampilkan nama tool + parameter utamanya
                    # agar user bisa lihat path/command apa yang sedang dijelajahi.
                    param_preview = _format_tool_params(
                        pending_tool.tool_name, pending_tool.tool_input
                    )
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
                executed.append(
                    (pending_tool, pending_tool.tool_id or f"call_{uuid.uuid4().hex[:12]}", result)
                )

            # Tulis KEMBALI giliran tool ke messages: (1) assistant yang MEMANGGIL tool,
            # (2) hasil tool. Sebelumnya HANYA hasil yang di-append — model (terutama
            # Gemma/DeepSeek lokal) tak melihat rekaman bahwa IA sendiri sudah memanggil
            # tool, jadi ia memanggil ULANG tool yang sama (§ user report: "terus menerus
            # write file"). Format internal bergaya OpenAI/Ollama; core/llm_client.py
            # menerjemahkannya per provider (tool_use/tool_result Anthropic,
            # functionCall/functionResponse Gemini). `id` memasangkan hasil ke
            # panggilannya; teks hop ini (tanpa markup tool call plain-text) ikut
            # disimpan agar penjelasan model sebelum memanggil tool tak hilang.
            if executed:
                hop_content, _ = LLMClient.parse_plaintext_tool_calls(hop_text)
                messages.append(
                    {
                        "role": "assistant",
                        "content": hop_content,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "function": {
                                    "name": call.tool_name,
                                    "arguments": call.tool_input,
                                },
                                **(
                                    {"thought_signature": call.tool_signature}
                                    if call.tool_signature
                                    else {}
                                ),
                            }
                            for call, call_id, _ in executed
                        ],
                    }
                )
                for call, call_id, result in executed:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": call.tool_name,
                            "content": _format_tool_result(call.tool_name, result),
                        }
                    )
            if stop:
                break
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

        # Policy Engine (TODO.md § Prioritas 3): kondisi TAMBAHAN di atas allow-list
        # & requires_approval statis — dicek SEBELUM approval/eksekusi (§ prasyarat
        # "runtime, bukan library" TREND.md). deny → tolak SEBELUM sempat memicu
        # approval sama sekali (lebih ketat, bukan cuma menunggu approval untuk
        # sesuatu yang memang harus ditolak mutlak).
        policy_decision = self.policy_engine.evaluate(name, input_data)
        if policy_decision.action == "deny":
            return {"error": f"Tool '{name}' ditolak oleh policy: {policy_decision.reason}"}
        policy_forces_approval = policy_decision.action == "require_approval"

        # Tool internal per-sesi (todo_write, report_blocker, set_workdir,
        # build_sandbox_image § Prioritas 8.3, sandbox_persist_enable § Fase 3):
        # suntik konteks sesi/role. Tool tak menerima ini via signature execute;
        # model tak perlu — & tak boleh — mengarang session_id/role (sumber
        # kebenaran = AgentLoop, bukan output model).
        if name in (
            "todo_write",
            "report_blocker",
            "set_workdir",
            "memory_search",
            "build_sandbox_image",
            "task_graph_submit",
            "sandbox_persist_enable",
        ):
            input_data = {
                **input_data,
                "_session_id": self.cfg.session_id,
                "_role": self.cfg.role,
            }
        # task_graph_submit (§ Task Graph) SEKALIGUS butuh identitas user pemilik
        # graph (task_graphs.owner_user_id) — tool lain di atas tak memakainya,
        # jadi tak ikut disuntik agar tak menambah field yang tak dipakai siapa pun.
        if name == "task_graph_submit":
            input_data = {**input_data, "_user_id": self.cfg.user_id}
        # Audit 2026-09-25 (#11): db_query butuh access_role user sesi (admin-only
        # saat auth aktif). Disuntik sistem — model tak bisa mengarangnya karena
        # nilai dari model ditimpa di sini.
        if name == "db_query":
            input_data = {**input_data, "_access_role": self.cfg.access_role}

        if tool.requires_approval or policy_forces_approval:
            # Autopilot (§1, §17): tidak ada manusia untuk approve → JANGAN eksekusi.
            # Antri sebagai proposal pending agar user meninjau nanti. Tanpa ini,
            # ApprovalGate.request() akan menggantung sampai timeout lalu DENY —
            # "rusak", bukan "aman". Di sini aman secara eksplisit: aksi destruktif
            # terjadwal jadi PROPOSAL, bukan eksekusi diam-diam.
            if self.cfg.autopilot:
                await self.approval.queue_proposal(
                    self.cfg.session_id,
                    name,
                    input_data,
                    task_id=self.cfg.task_id,
                    node_id=self.cfg.node_id,
                )
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
            # `not policy_forces_approval` adalah defense-in-depth KEDUA (sama pola
            # _TRUST_MODE_EXEMPT — dicek independen di titik status-emission
            # _run_tool_loop DAN di titik eksekusi ini): bila caller keliru meneruskan
            # bypass_approval=True untuk tool yang di-force approval oleh policy,
            # jalur ini tetap menolak bypass — policy tak boleh bergantung SEMATA
            # pada caller menghitung dengan benar.
            if (
                bypass_approval
                and not _trust_mode_exempt(name, input_data)
                and not policy_forces_approval
            ):
                approved = await self.approval.auto_approve(
                    self.cfg.session_id,
                    name,
                    input_data,
                    agent_identity=self.agent_identity,
                    task_id=self.cfg.task_id,
                    node_id=self.cfg.node_id,
                )
            else:
                # Audit produksi 2026-07-29: teruskan identitas user (bila ada,
                # "default" = auth nonaktif/tak ada user login) agar web/main.py
                # bisa menggerbangi GET /approvals & POST /approve per-user.
                approved = await self.approval.request(
                    self.cfg.session_id,
                    name,
                    input_data,
                    approval_id=approval_id,
                    owner_user_id=self.cfg.user_id if self.cfg.user_id != "default" else None,
                    agent_identity=self.agent_identity,
                    task_id=self.cfg.task_id,
                    node_id=self.cfg.node_id,
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
                self.cfg.session_id,
                self.cfg.role,
                name,
                outcome,
                latency_ms,
                user_id=self.cfg.user_id,
                task_id=self.cfg.task_id,
                node_id=self.cfg.node_id,
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
        return _soul_allows_tool(self._soul, name)

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
                generator_model=generator_model_of(turn),
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
