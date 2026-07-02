from dataclasses import dataclass, field
import os

from infra.env import load_dotenv

# Muat `.env` SEBELUM CONFIG dibaca dari os.environ (lihat infra/env.py).
load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """Semua konfigurasi global. frozen=True agar tidak bisa diubah setelah init."""

    db_path: str = "data/openclawn.db"
    ollama_base: str = "http://localhost:11434"
    anthropic_base: str = "https://api.anthropic.com"
    gemini_base: str = "https://generativelanguage.googleapis.com"
    # Self-host auth (§P0 production-readiness): password shared satu-satunya user.
    # Kosong (default) → auth DIMATIKAN, perilaku lama tetap jalan tanpa login
    # (aman untuk localhost dev). Isi di .env untuk self-host di VPS publik.
    auth_token: str = ""
    # Idle timeout (opt-in, TODO.md § Prioritas 1.5): logout otomatis setelah N
    # detik TAK aktif — berbeda dari SESSION_MAX_AGE_SEC (absolute expiry 7 hari
    # sejak login, tetap berlaku sebagai batas atas walau idle timeout aktif).
    # None (default) → OFF, perilaku lama (hanya absolute expiry) tak berubah.
    # Nilai jual untuk buyer dengan kebijakan sesi ketat (bank/finance/compliance).
    idle_timeout_sec: int | None = None
    max_context_tokens: int = 28_000
    max_tool_hops: int = 5
    # Output cap default per hop LLM (§ stream_with_fallback). Turn dengan tools
    # tersedia (hop bertool) butuh ruang lebih (llm_max_tokens_with_tools) —
    # § user report: model reasoning-heavy (Gemma <think>) kehabisan giliran
    # SAAT MASIH merencanakan tool mana yang dipakai (instruksi format→tool
    # pm/dev/qa relatif detail), sebelum sempat bertindak/menjawab, dengan cap
    # lama (4096). Cap dinaikkan HANYA saat tools_schema dikirim — turn tanpa
    # tool (mis. ringkas percakapan di _maybe_compact) tetap pakai default lama.
    llm_max_tokens_default: int = 4096
    llm_max_tokens_with_tools: int = 8192
    llm_max_retries: int = 3
    approval_timeout_sec: int = 120
    decay_interval_sec: int = 3600
    skill_decay_base: float = 0.97
    skill_archive_threshold: float = 0.3
    skill_revive_boost: float = 0.5
    max_active_skills: int = 8
    confidence_threshold: int = 4
    # Memori jangka panjang: arsipkan sesi ke L4 setelah melewati ambang turn ini
    # (cukup bermakna untuk dicari lagi lintas sesi, tapi tidak tiap turn).
    archive_after_turns: int = 6
    # Jumlah giliran (user/assistant) TERBARU sesi ini yang dimuat kembali ke history
    # tiap request (AgentLoop dibuat baru → history kosong). build() lalu memangkas
    # lagi sesuai budget token; ini batas atas agar sesi panjang tak membanjiri query.
    session_history_turns: int = 20
    # === Compounding intelligence (Sprint 6-8) ===
    # I1 — Skill Curator: gabung skill mirip agar library tak terfragmentasi.
    # Jauh lebih jarang dari decay (1×/hari); gated oleh judge & similarity.
    curation_interval_sec: int = 86_400
    curation_similarity_threshold: float = 0.78  # ambang pre-filter leksikal
    curation_max_pairs_per_pass: int = 5  # batasi biaya LLM judge per pass
    curation_judge_min_confidence: int = 4  # merge hanya bila judge ≥ 4/5
    curation_auto: bool = False  # §8: default usulan-saja, user apply di /skills
    # I2 — Draft promotion: draft yang terbukti berguna naik 'active'.
    draft_promote_uses: int = 3  # dipakai-sukses N kali → promote
    # Draft cleanup: draft yang TUA & tak pernah terbukti (draft_success_count=0)
    # diarsipkan saat decay pass — cegah menumpuk. ARSIP (bukan hapus): konsisten
    # prinsip "tak ada kehilangan data senyap". 0 = nonaktifkan cleanup.
    draft_stale_days: int = 14
    # I3 — Skill refine on correction: perbaiki skill yang menyesatkan (versioned).
    refine_on_correction: bool = True
    refine_max_per_pass: int = 3
    # I4 — Calibration auto-apply: router menyetel diri DALAM rem (opt-in, §8).
    calibration_auto_apply: bool = False  # default aman: tetap manual
    calibration_auto_max_step: int = 1  # clamp ±1, tak pernah melompat
    calibration_auto_interval_sec: int = 86_400
    calibration_auto_min_sample: int = 20  # jangan menyetel dari noise
    # I5 — Dialectic user model (opsional): profil user naratif lintas sesi.
    user_model_enabled: bool = False
    user_model_interval_sec: int = 86_400
    # Keyword routing (§1.5: locale TIDAK boleh hardcoded di core). Default ID+EN;
    # tambahkan keyword bahasa lain di sini atau lewat soul.toml [routing] tiap role
    # (router menggabungkan default + soul). Query non-ID/EN tetap dirute oleh sinyal
    # netral-bahasa (panjang query/history) walau keyword tak cocok — degrade anggun.
    routing_tech_keywords: tuple = field(
        default_factory=lambda: (
            "code",
            "debug",
            "review",
            "arsitektur",
            "architecture",
            "implement",
            "refactor",
            "query",
            "database",
            "api",
            "deploy",
            "bug",
        )
    )
    routing_multistep_keywords: tuple = field(
        default_factory=lambda: (
            "analisis",
            "analyze",
            "bandingkan",
            "compare",
            "rencana",
            "plan",
            "langkah",
            "step",
            "strategi",
            "strategy",
            "breakdown",
            "jelaskan detail",
            "explain in detail",
            "evaluasi",
            "evaluate",
        )
    )
    routing_urgency_keywords: tuple = field(
        default_factory=lambda: ("urgent", "segera", "deadline", "asap", "penting", "important")
    )
    # Multibahasa lapis 2 — kapabilitas bahasa model (bukan kompleksitas):
    # script (sistem tulisan) yang DIANGGAP kuat di tier lokal kecil. Query di luar
    # daftar ini → naikkan tier (model cloud umumnya lebih multibahasa). Opt-in:
    # default OFF agar tak menaikkan biaya tanpa diminta. `latin` mencakup ID/EN/ES/dst.
    routing_language_bump: bool = False
    routing_local_scripts: tuple = field(default_factory=lambda: ("latin",))
    # Workspace root: semua tool file (read/write/edit/glob/grep/list_dir) dibatasi
    # ke folder ini. Path di luar root ditolak (anti ../ & symlink escape). Keamanan #1.
    workspace_root: str = "."
    # Batas hasil tool agar tidak membanjiri context (token-first §1.4).
    tool_max_output: int = 10_000
    # Timeout keras per eksekusi tool (§1.3 kegagalan anggun): tool yang menggantung
    # (network, DB lock) tidak boleh membekukan turn. code_run/shell_run punya timeout
    # sandbox sendiri 30s, jadi batas ini sedikit di atasnya agar tidak memotong sandbox.
    tool_timeout_sec: int = 40
    # === Headroom compaction (opt-in via /settings, terinspirasi chopratejas/headroom) ===
    # Saat budget token habis, compactor default MEMOTONG turn lama (truncation — yang
    # hilang benar-benar hilang, tapi jujur). Compaction MERINGKAS turn lama jadi satu
    # blok alih-alih membuang — hemat token tanpa kehilangan konteks total (§1.4).
    # OPT-IN & default OFF: peringkasan via LLM bisa membuang nuansa & menambah latensi/
    # biaya; truncation tetap default aman. Mode disimpan di /settings: off|local|cloud.
    # `local` = tier lokal ringan (gratis/privat); `cloud` = fallback chain (kualitas
    # untuk history kompleks, bisa naik ke cloud). Default mode di sini hanya dipakai bila
    # /settings kosong.
    compaction_default_mode: str = "off"  # off | local | cloud
    # Model lokal untuk meringkas saat mode=local (ekstraktif, model kecil cukup).
    compaction_local_model: tuple = field(default_factory=lambda: ("ollama", "gemma4:e2b"))
    # Sisakan minimal N turn terbaru UTUH (jangan ringkas yang baru — paling relevan).
    compaction_keep_recent: int = 4
    # Hanya ringkas bila ada cukup turn lama untuk dipadatkan (hindari LLM call sia-sia).
    compaction_min_old_turns: int = 3
    # Multi-agent conversation: batasi total giliran agar tidak loop tak berujung
    # & token blowout (pola sama max_tool_hops). Ronde default untuk debate.
    max_conversation_turns: int = 12
    debate_default_rounds: int = 2
    conversation_default_participants: tuple = field(default_factory=lambda: ("pm", "dev", "qa"))
    # fallback chain: urutan model jika provider utama gagal
    # Fallback chain LOKAL-dulu, urut per kapasitas (selaras MODELS router):
    # gemma4:e4b (ringan) → deepseek-r1 → qwen3.5:9b (paling mampu lokal), lalu
    # Gemini (cloud) sebagai pengaman terakhir bila semua lokal gagal load.
    fallback_chain: tuple = field(
        default_factory=lambda: (
            ("ollama", "gemma4:e4b"),
            ("ollama", "deepseek-r1:latest"),
            ("ollama", "qwen3.5:9b"),
            ("gemini", "gemini-2.5-flash"),
        )
    )

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            db_path=os.environ.get("OPENCLAWN_DB", "data/openclawn.db"),
            ollama_base=os.environ.get("OLLAMA_BASE", "http://localhost:11434"),
            anthropic_base=os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com"),
            gemini_base=os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com"),
            workspace_root=os.environ.get("OPENCLAWN_WORKSPACE", "."),
            auth_token=os.environ.get("OPENCLAWN_AUTH_TOKEN", ""),
            idle_timeout_sec=(
                int(os.environ["OPENCLAWN_IDLE_TIMEOUT_SEC"])
                if os.environ.get("OPENCLAWN_IDLE_TIMEOUT_SEC")
                else None
            ),
        )


# Singleton global — di-inject ke semua modul via dependency injection
CONFIG = AppConfig.from_env()
