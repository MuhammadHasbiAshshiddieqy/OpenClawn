from dataclasses import dataclass, field
import os
import secrets

from infra.env import load_dotenv

# Muat `.env` SEBELUM CONFIG dibaca dari os.environ (lihat infra/env.py).
load_dotenv()


@dataclass(frozen=True)
class AppConfig:
    """Semua konfigurasi global. frozen=True agar tidak bisa diubah setelah init."""

    db_path: str = "data/openclawn.db"
    # Anchoring untuk audit chain (TODO.md § Prioritas 9.1 follow-up) — file
    # TERPISAH dari db_path, sengaja: verify() rantai sendirian tak menangkap
    # truncation/rewrite penuh (lihat core/audit_chain.py); anchor di file lain
    # (JSON Lines, append-only) yang bisa disalin off-host operator menutup
    # celah itu. Lihat core/audit_anchor.py + scripts/anchor_audit_chain.py.
    audit_anchor_path: str = "data/audit_anchors.jsonl"
    ollama_base: str = "http://localhost:11434"
    anthropic_base: str = "https://api.anthropic.com"
    gemini_base: str = "https://generativelanguage.googleapis.com"
    # Self-host auth (§P0 production-readiness): password shared satu-satunya user.
    # Kosong (default) → auth DIMATIKAN, perilaku lama tetap jalan tanpa login
    # (aman untuk localhost dev). Isi di .env untuk self-host di VPS publik.
    auth_token: str = ""
    # OAuth2/OIDC (TODO.md § Prioritas 5) — mode auth TAMBAHAN, bukan pengganti
    # shared-secret di atas. Kosong (default) → OIDC DIMATIKAN, perilaku lama
    # (shared-secret atau tanpa auth) tak berubah sama sekali. Operator pilih
    # SATU provider generik yang kompatibel dengan Google/Microsoft/Okta/dsb
    # via discovery document standar (`{issuer}/.well-known/openid-configuration`)
    # — bukan integrasi vendor-spesifik, konsisten prinsip "no SDK vendor" (§1.6:
    # yang dilarang adalah SDK vendor-LLM, OIDC adalah protokol terbuka seperti MCP).
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Base URL publik server ini, dipakai membangun redirect_uri callback
    # (`{oidc_redirect_base}/auth/callback`) — perlu eksplisit karena self-host
    # di belakang reverse proxy/domain kustom, tak bisa diasumsikan dari request.
    oidc_redirect_base: str = "http://localhost:8000"
    # Audit 2026-09-25 (#12): siapa yang boleh login via OIDC. Kosong keduanya →
    # semua akun yang lolos di IdP (perilaku lama, di-log sebagai peringatan).
    # Env: OPENCLAWN_OIDC_ALLOWED_EMAILS / OPENCLAWN_OIDC_ALLOWED_DOMAINS (koma).
    oidc_allowed_emails: tuple = ()
    oidc_allowed_domains: tuple = ()
    # Keputusan 2026-09-26 (secure-by-default, non-breaking): tanpa allowlist,
    # pendaftaran akun OIDC BARU ditutup — user yang sudah ada tetap bisa login
    # dan user pertama tetap bootstrap admin. `True` = perilaku lama (akun APA
    # PUN yang lolos di IdP jadi member). Env: OPENCLAWN_OIDC_OPEN_SIGNUP=true.
    oidc_open_signup: bool = False
    # Secret HMAC untuk menandatangani cookie sesi (`create_session_token`/
    # `verify_session_token`, security/auth.py). SEBELUM OIDC ada, `auth_token`
    # dipakai langsung sebagai secret (aman karena hanya SATU deployment shared-
    # secret yang tahu nilainya). Dengan OIDC, operator bisa memilih TAK mengisi
    # `auth_token` sama sekali (login HANYA lewat provider) — di situ `auth_token`
    # kosong tak bisa jadi secret sesi. `OPENCLAWN_SESSION_SECRET` eksplisit
    # menutup celah ini; bila juga kosong, di-generate acak saat boot (aman,
    # tapi restart server akan me-logout semua sesi aktif — operator yang ingin
    # sesi bertahan lintas-restart HARUS mengisi salah satu secret eksplisit).
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # OpenConnector (opsional, third-party — github.com/oomol-lab/open-connector,
    # Apache-2.0) — URL dashboard-nya untuk link entry point di sidebar. Kosong
    # (default) → link TIDAK ditampilkan (integrasi opt-in, konsisten docker-compose.yml
    # § connector yang juga opt-in via profile). Isi bila container `connector`
    # dijalankan — localhost:3000 untuk dev, subdomain publik untuk deployment
    # dengan Caddyfile.example § connector.example.com.
    connector_url: str = ""
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
    # Skill Marketplace lintas-role (TODO.md § Prioritas 6): batas skill role LAIN
    # (visibility shared/inherited) yang ikut disuntik — token-first §1.4, lebih
    # kecil dari max_active_skills karena skill role sendiri selalu lebih relevan.
    max_shared_skills: int = 3
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
    # Audit 2026-09-25 (#1, kritis): allowlist folder yang BOLEH dipilih sebagai
    # folder kerja per-sesi (field UI `workdir` / tool `set_workdir`). Sebelumnya
    # path APA PUN diterima (termasuk `/`) — user login mana pun bisa membaca
    # `/proc/self/environ`/DB lewat tool file tanpa approval. Kosong → default
    # `infra/workspace.py::default_workdir_roots` (home + workspace_root bila auth
    # nonaktif; TIDAK ADA override sama sekali bila auth aktif). Env:
    # `OPENCLAWN_WORKDIR_ROOTS` (dipisah `os.pathsep`, mis. `/srv/a:/srv/b`).
    workdir_allowed_roots: tuple = ()
    # Audit 2026-09-25 (#3): allowlist nama env var yang boleh dipakai sebagai
    # `vault:KEY` di header http_request. Kosong → semua KECUALI credential
    # aplikasi sendiri (`OPENCLAWN_*`, API key LLM/Tavily — lihat tools/web.py).
    # Env: `OPENCLAWN_HTTP_VAULT_KEYS` (dipisah koma).
    http_vault_allowed_keys: tuple = ()
    # Audit 2026-09-25 (kritis): Host header tambahan yang diterima SAAT AUTH
    # NONAKTIF (selain localhost/127.0.0.1/::1). Tanpa auth, server hanya aman
    # diakses dari mesin itu sendiri — domain penyerang yang di-resolve ke
    # 127.0.0.1 (DNS rebinding) dianggap same-origin oleh browser dan bisa
    # mengendalikan agent. Env: `OPENCLAWN_ALLOWED_HOSTS` (koma, tanpa port).
    allowed_hosts: tuple = ()
    # Batas hasil tool agar tidak membanjiri context (token-first §1.4).
    tool_max_output: int = 10_000
    # Timeout keras per eksekusi tool (§1.3 kegagalan anggun): tool yang menggantung
    # (network, DB lock) tidak boleh membekukan turn. code_run/shell_run punya timeout
    # sandbox sendiri 30s, jadi batas ini sedikit di atasnya agar tidak memotong sandbox.
    tool_timeout_sec: int = 40
    # § IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 5 (runtime isolasi
    # pluggable — owner memilih ini, BUKAN Fase 3 sandbox lifecycle, TANPA
    # Firecracker/microVM yang direkomendasikan dilewati eksplisit). Runtime
    # container untuk `code_run`/`shell_run` (`tools/sandbox.py`) — default
    # `"runc"` (bawaan Docker, TIDAK diteruskan sebagai `--runtime` sama sekali,
    # perilaku lama tak berubah). Isi `"runsc"` (gVisor) untuk isolasi lebih kuat
    # di deployment yang butuh (enterprise/multi-tenant) — HANYA satu flag Docker,
    # bukan perubahan arsitektur; operator bertanggung jawab memastikan runtime
    # itu benar-benar terpasang & terdaftar di Docker daemon-nya (`docker info
    # --format '{{.Runtimes}}'`), tak diverifikasi kode ini. TIDAK berlaku untuk
    # `docker build` (`build_project_image`) — beda skop, isolasi build-time
    # sudah punya alasan lain (network sengaja terbuka SAAT build saja).
    sandbox_runtime: str = "runc"
    # Keputusan 2026-09-26 (sandbox di docker-compose via Docker-in-Docker):
    # direktori tempat run_python menaruh skrip sementara. Bila daemon Docker
    # BUKAN di host yang sama (DinD sidecar), path ini harus volume bersama yang
    # di-mount di path SAMA pada app & daemon. None → temp default OS.
    # Env: OPENCLAWN_SANDBOX_TMPDIR.
    sandbox_tmp_dir: str | None = None
    # § IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 3 (sandbox
    # lifecycle) — owner disetujui EKSPLISIT via AskUserQuestion setelah
    # trade-off keamanan dijelaskan (lihat `infra/sandbox_lifecycle.py`).
    # Idle → pause (hemat CPU, container masih ada); lebih lama tak dipakai →
    # destroy (container + volume dihapus fisik). `sandbox_persist_max_containers`
    # menutup permukaan DoS baru yang model ephemeral lama tak punya — banyak
    # sesi opt-in sekaligus tak boleh membebani host tanpa batas.
    sandbox_persist_idle_ttl_sec: int = 600
    sandbox_persist_destroy_ttl_sec: int = 3600
    sandbox_persist_max_containers: int = 5
    sandbox_reaper_tick_sec: int = 60
    # === Task Graph (DAG subtask, § IMPROVEMENT-Sandbox-Isolation-Parallelization.md
    # Fase 1+2) — core/task_graph.py + core/task_executor.py ===
    # Batas subtask BERSAMAAN dalam satu graph — konservatif karena tiap subtask
    # adalah AgentLoop penuh (bisa memanggil code_run/sandbox sendiri); default
    # lebih tinggi berisiko membebani host tanpa manfaat nyata untuk single-host self-host.
    task_graph_max_concurrency: int = 3
    # Percobaan maksimum per node sebelum menyerah permanen (status='failed').
    # INI SEKALIGUS breaker-nya — tak ada abstraksi circuit-breaker terpisah,
    # lihat docstring core/task_executor.py.
    task_graph_max_node_attempts: int = 3
    # Backoff dasar antar percobaan (eksponensial: base * 2^(attempt-1)).
    task_graph_retry_backoff_sec: float = 2.0
    # Timeout keras per node (§1.3) — subtask yang menggantung tak boleh
    # membekukan seluruh graph selamanya.
    task_graph_node_timeout_sec: int = 300
    # Audit 2026-09-25: budget TOTAL satu graph (`task_graph_submit` dijalankan
    # di dalam `AgentLoop._execute_tool`). Sebelumnya tool ini ikut
    # `tool_timeout_sec` (40s) — jauh di bawah timeout per node (300s), jadi graph
    # nyata hampir selalu dipotong di 40s: baris task_graphs macet 'running'
    # selamanya dan subtask tetap jalan tanpa induk.
    task_graph_timeout_sec: int = 1800
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

    @property
    def auth_active(self) -> bool:
        """True bila SALAH SATU mode auth (shared-secret ATAU OIDC) aktif —
        middleware harus menegakkan sesi. Beda dari `bool(auth_token)` lama yang
        tak tahu soal OIDC-only (operator bisa isi OIDC tanpa auth_token sama sekali)."""
        return bool(self.auth_token) or bool(
            self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret
        )

    @classmethod
    def from_env(cls) -> "AppConfig":
        # session_secret: auth_token dulu (kompatibilitas mundur — deployment shared-
        # secret existing tak berubah), lalu OPENCLAWN_SESSION_SECRET eksplisit
        # (dibutuhkan untuk OIDC-only agar sesi tahan restart), baru fallback acak
        # (default dataclass field bila kedua env kosong).
        auth_token = os.environ.get("OPENCLAWN_AUTH_TOKEN", "")
        explicit_session_secret = os.environ.get("OPENCLAWN_SESSION_SECRET", "")
        kwargs = dict(
            db_path=os.environ.get("OPENCLAWN_DB", "data/openclawn.db"),
            audit_anchor_path=os.environ.get(
                "OPENCLAWN_AUDIT_ANCHOR_PATH", "data/audit_anchors.jsonl"
            ),
            ollama_base=os.environ.get("OLLAMA_BASE", "http://localhost:11434"),
            anthropic_base=os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com"),
            gemini_base=os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com"),
            workspace_root=os.environ.get("OPENCLAWN_WORKSPACE", "."),
            workdir_allowed_roots=tuple(
                p.strip()
                for p in os.environ.get("OPENCLAWN_WORKDIR_ROOTS", "").split(os.pathsep)
                if p.strip()
            ),
            allowed_hosts=tuple(
                h.strip().lower()
                for h in os.environ.get("OPENCLAWN_ALLOWED_HOSTS", "").split(",")
                if h.strip()
            ),
            http_vault_allowed_keys=tuple(
                k.strip()
                for k in os.environ.get("OPENCLAWN_HTTP_VAULT_KEYS", "").split(",")
                if k.strip()
            ),
            sandbox_runtime=os.environ.get("OPENCLAWN_SANDBOX_RUNTIME", "runc"),
            sandbox_tmp_dir=os.environ.get("OPENCLAWN_SANDBOX_TMPDIR") or None,
            auth_token=auth_token,
            oidc_issuer=os.environ.get("OPENCLAWN_OIDC_ISSUER", ""),
            oidc_client_id=os.environ.get("OPENCLAWN_OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.environ.get("OPENCLAWN_OIDC_CLIENT_SECRET", ""),
            oidc_allowed_emails=tuple(
                e.strip().lower()
                for e in os.environ.get("OPENCLAWN_OIDC_ALLOWED_EMAILS", "").split(",")
                if e.strip()
            ),
            oidc_allowed_domains=tuple(
                d.strip().lower().lstrip("@")
                for d in os.environ.get("OPENCLAWN_OIDC_ALLOWED_DOMAINS", "").split(",")
                if d.strip()
            ),
            oidc_open_signup=os.environ.get("OPENCLAWN_OIDC_OPEN_SIGNUP", "").strip().lower()
            in ("1", "true", "yes"),
            oidc_redirect_base=os.environ.get(
                "OPENCLAWN_OIDC_REDIRECT_BASE", "http://localhost:8000"
            ),
            connector_url=os.environ.get("OPENCLAWN_CONNECTOR_URL", ""),
            idle_timeout_sec=(
                int(os.environ["OPENCLAWN_IDLE_TIMEOUT_SEC"])
                if os.environ.get("OPENCLAWN_IDLE_TIMEOUT_SEC")
                else None
            ),
        )
        if auth_token:
            kwargs["session_secret"] = auth_token
        elif explicit_session_secret:
            kwargs["session_secret"] = explicit_session_secret
        return cls(**kwargs)


# Singleton global — di-inject ke semua modul via dependency injection
CONFIG = AppConfig.from_env()
