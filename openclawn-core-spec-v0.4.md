# OpenCLAWN Core — Implementation Spec v0.4

> **Mission:** Control plane untuk AI agent — ringan, aman, self-improving, dan dirancang untuk bermanfaat bagi siapa pun, bukan terikat satu konteks bisnis.
>
> **Status:** Production-track (self-host). Rilis berjalan: **v0.12.0** (2026-08-02).
> **Interface:** Web UI (FastAPI + HTMX, SSE streaming)
> **LLM:** Hybrid (Ollama lokal + Gemini/Claude cloud), dengan fallback chain
> **Bahasa:** Python 3.12

**Perbedaan dari v0.3:** v0.3 adalah *blueprint* yang dibawa ke repository kosong untuk membangun dari nol (Sprint 0–4). Dokumen itu sudah selesai tugasnya — kodenya sudah ada, dan sudah tumbuh jauh melewati apa yang v0.3 gambarkan (multi-tenant, RBAC, OIDC, Policy Engine, Event-Driven Runtime, 5 role, ~28 tool, dst — lihat §17 Roadmap untuk riwayat lengkap). **v0.4 adalah spec yang sama sumber-kebenarannya, tapi perannya bergeser: bukan lagi blueprint greenfield, melainkan peta arsitektur & kontrak yang mengikat kondisi kode SEKARANG.** Detail implementasi per-baris didelegasikan ke `docs/*.md` (yang di-update tiap perubahan publik, CLAUDE.md §11) dan ke kode itu sendiri — spec ini fokus ke *apa yang harus benar*, bukan mereproduksi source.

v0.3 tetap disimpan (`openclawn-core-spec-v0.3.md`) sebagai catatan sejarah Sprint 0–4 — jangan dihapus, tapi jangan lagi dianggap mencerminkan kode saat ini.

---

## Daftar Isi

1. [Filosofi & Tujuan](#1-filosofi--tujuan)
2. [Positioning: Agent Control Plane](#2-positioning-agent-control-plane)
3. [4 Inovasi Inti](#3-4-inovasi-inti)
4. [Arsitektur Keseluruhan](#4-arsitektur-keseluruhan)
5. [Stack & Dependencies](#5-stack--dependencies)
6. [Struktur Direktori](#6-struktur-direktori)
7. [Konfigurasi (`infra/config.py`)](#7-konfigurasi-infraconfigpy)
8. [Database Schema](#8-database-schema)
9. [Modul Inti — `core/`](#9-modul-inti-core)
10. [Modul Memori — `memory/`](#10-modul-memori-memory)
11. [Peran & Kontrak — `roles/`](#11-peran--kontrak-roles)
12. [Tools + Sandbox — `tools/`](#12-tools--sandbox-tools)
13. [Security Layer — `security/` + `infra/users.py`](#13-security-layer-security--infrausenspy)
14. [Multi-Tenant](#14-multi-tenant)
15. [Web UI — `web/`](#15-web-ui-web)
16. [Testing Strategy](#16-testing-strategy)
17. [Roadmap & Riwayat Rilis](#17-roadmap--riwayat-rilis)
18. [Quick Start](#18-quick-start)
19. [Lampiran A: Audit Resolution v0.3 (historis)](#lampiran-a-audit-resolution-v03-historis)
20. [Lampiran B: Pengecualian Sadar Dependency](#lampiran-b-pengecualian-sadar-dependency)
21. [Lampiran C: Peta Cepat "Mau Kerjakan Apa"](#lampiran-c-peta-cepat-mau-kerjakan-apa)

---

## 1. Filosofi & Tujuan

| Prinsip | Implikasi konkret |
|---|---|
| **Keamanan dulu** | `code_run` WAJIB Docker sandbox (`--network none`, `--read-only`, non-root, timeout). Tidak ada eksekusi kode di host, tanpa pengecualian. |
| **Credential tidak pernah masuk context/prompt** | Hanya diinjeksi saat outbound request via `Vault`. Tidak pernah di-log, tidak pernah masuk tabel DB (kecuali terenkripsi — lihat §13). |
| **Resilient by default** | Setiap dependency eksternal (Ollama, Gemini, Anthropic, OIDC provider) punya retry, fallback, atau graceful degradation. Ollama offline ≠ agent mati. |
| **Token-first** | Target context < 28K token (`CONFIG.max_context_tokens`). Prompt caching aktif untuk system prompt. |
| **Universal, bukan personal** | Tidak ada hardcoded domain/locale di core. Locale via field eksplisit, keyword routing per-bahasa via config, bukan dipaksakan satu bahasa. |
| **Setiap inovasi = modul terpisah** | 4 inovasi inti (§3) bisa di-*extract* jadi paket standalone — tidak boleh disederhanakan atau dipangkas demi kemudahan implementasi. |

Urutan di atas bermakna: bila dua prinsip bertabrakan, yang lebih atas menang. Detail lengkap tiap prinsip (termasuk kasus konkret dari audit produksi) ada di `CLAUDE.md §1`.

---

## 2. Positioning: Agent Control Plane

OpenCLAWN bukan sekadar *agent framework* (cara agent memanggil tool & berkoordinasi) — itu sudah jadi *table stakes* di ekosistem (LangChain, CrewAI, AutoGen semuanya menyediakannya). OpenCLAWN adalah **control plane**: lapisan yang menjawab "apakah tindakan agent ini diizinkan, dihentikan untuk direview manusia bila perlu, dan tercatat — *sebelum* terjadi, bukan sesudah dicek log."

Tiga pilar yang membedakan posisi ini:

| Pilar | Implementasi |
|---|---|
| **Policy-Before-Dispatch** | `PolicyEngine` (§13) dievaluasi di **dua titik independen** sebelum tool dieksekusi — bukan setelah fakta. |
| **Human-Approval Checkpoints** | `ApprovalGate` (§13) benar-benar **memblokir** loop agent sampai user memutuskan (atau timeout → deny) — bukan baris log pasif. |
| **Immutable Audit Evidence** | `RoutingAuditor` (§9) mencatat keputusan **sebelum** LLM dipanggil dan melengkapinya **sesudah** — jejak yang tak bisa direkonstruksi ulang setelah kejadian. |

Riset adopsi enterprise 2026 mencatat 72% organisasi sudah menjalankan agentic AI di produksi, tapi hanya 21% dengan model governance yang matang — gap inilah yang jadi alasan ketiga pilar di atas dianggap inti, bukan fitur tambahan.

---

## 3. 4 Inovasi Inti

| # | Inovasi | Masalah yang dipecahkan | File utama |
|---|---|---|---|
| **1** | **Routing audit + self-calibration** | Tidak ada agent yang mencatat *mengapa* routing dibuat dan apakah terbukti tepat | `core/router.py`, `core/audit.py`, `core/calibration.py` |
| **2** | **Skill decay + relevance aging** | Skill tree menumpuk selamanya, konteks tercemar skill basi | `memory/skill_decay.py` |
| **3** | **Confidence-gated crystallization** | Self-evolving agent menyimpan skill dari solusi buruk | `core/crystallizer.py` |
| **4** | **Role output contracts** | Multi-agent workflow tanpa typed contract → fragile | `roles/contracts.py`, `roles/registry.py` |

Sejak v0.4.0-alpha, keempatnya diperluas jadi **Compounding Intelligence**: skill yang dikoreksi user memicu `refine_on_correction` (I3, §9), skill yang sering dipakai dikonsolidasi `SkillCuratorManager` (I1, §10), dan feedback antar-turn dijembatani `SkillFeedback` (§10) — closed-loop, bukan cuma catat-dan-lupa.

---

## 4. Arsitektur Keseluruhan

```
┌──────────────────────────────────────────────────────────────────────┐
│  WEB UI (FastAPI + HTMX + SSE) — auth (shared-secret|OIDC) + RBAC     │
│  chat · /metrics · /skills · /router · /settings · /admin/users      │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
┌──────────────────────────────────▼───────────────────────────────────┐
│                              AGENT LOOP                               │
│  perceive → route → policy check → call LLM → [approval] → execute   │
│  tool → policy check (2) → write memory → post-turn                  │
│  (tool loop: ITERATIVE `while`, bukan rekursif)                      │
└───┬────────┬─────────┬──────────┬──────────┬──────────┬─────────────┘
   │        │         │          │          │          │
┌───▼───┐ ┌──▼───┐ ┌────▼────┐ ┌───▼────┐ ┌───▼─────┐ ┌──▼────────┐
│ROUTER │ │MEMORY│ │ SKILLS  │ │ ROLES  │ │APPROVAL │ │  POLICY   │
│soul-  │ │L1-L4 │ │+DECAY   │ │+CONTRACT│ │ gate    │ │  ENGINE   │
│aware  │ │FTS5  │ │[#2]     │ │ [#4]   │ │ (HITL)  │ │(TOML/dict)│
│[#1]   │ └──────┘ └────┬────┘ └────────┘ └─────────┘ └───────────┘
└───┬───┘               │
   │            ┌────────▼────────┐          ┌───────────────────┐
┌───▼─────┐     │  CRYSTALLIZER   │          │  GUARDRAIL ENGINE  │
│ AUDIT   │     │  +CONFIDENCE    │          │  (input/output     │
│+CALIB   │     │  evaluator≥gen  │          │   rail, ala NeMo)  │
│ [#1]    │     │     [#3]        │          └────────────────────┘
└─────────┘     └─────────────────┘
┌────────────────────────────────────────────────────────────────────┐
│  SHARED INFRA: DatabaseManager (tenant-aware) · AppConfig · Vault   │
│  UserStore (RBAC) · ChatSessionStore · EventBus (agent_events)     │
└────────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────┐
│  LLM CLIENT: retry + backoff + fallback chain                       │
│  Ollama (lokal) ↔ Gemini / Anthropic (cloud) · prompt caching       │
└────────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────┐
│  SANDBOX: code_run & shell_run dalam Docker                         │
│  (--network none · --read-only · --user nobody · timeout)          │
└────────────────────────────────────────────────────────────────────┘
```

**Jalur satu request** (ringkas — detail lengkap di `docs/core.md` § `AgentLoop`):

1. `web/main.py::chat_stream` menerima pesan → bangun/ambil `AgentLoop` untuk sesi.
2. `RoutingAuditor.check_correction` — deteksi apakah turn *sebelumnya* dikoreksi user.
3. `SkillDecayManager.get_active_skills` + `MemoryManager.load_context` — bangun konteks (L1–L4 + skill aktif).
4. `SmartRouter.decide` — skor 8 dimensi → tier → model (via `RouterConfigStore.get_map()` bila di-override).
5. `RoutingAuditor.log_decision` — dicatat **sebelum** LLM dipanggil.
6. `_run_tool_loop` (iteratif): stream LLM → bila ada tool call → `PolicyEngine.evaluate` (titik 1) → `ApprovalGate.request` bila perlu → `PolicyEngine.evaluate` lagi (titik 2, defense-in-depth) → eksekusi tool → lanjut hop berikutnya.
7. `RoutingAuditor.finalize` — lengkapi token/biaya/latensi.
8. `_post_turn` (background, dengan `add_done_callback` untuk error logging): tulis L1 checkpoint, arsip L4 bila perlu, `ConfidenceCrystallizer.crystallize` bila ≥3 tool call, `SkillCuratorManager` bila ambang konsolidasi tercapai.

---

## 5. Stack & Dependencies

```toml
[project]
name = "openclawn"
version = "0.12.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110", "uvicorn[standard]>=0.29", "httpx>=0.27",
    "aiosqlite>=0.20", "jinja2>=3.1", "python-multipart>=0.0.9",
    "pydantic>=2.6", "structlog>=24.1", "tenacity>=8.2", "aiofiles>=23.2",

    "pypdf>=4.0",                 # pdf_read — murni-Python
    "python-docx>=1.1",           # doc_write (docx)
    "python-pptx>=0.6",           # doc_write (pptx)
    "openpyxl>=3.1",              # doc_write (xlsx)
    "reportlab>=4.0",             # pdf_write

    "mcp>=1.0",                   # klien Model Context Protocol (protokol terbuka)
    "pyyaml>=6.0",                # clawn.yaml manifest (writer YAML)
    "authlib>=1.7",               # OAuth2/OIDC login (httpx-based, JWKS via joserfc)
    "cryptography>=42.0",         # enkripsi-at-rest (Fernet) untuk mcp_servers.env
    "regex>=2023.0",              # GrepTool — timeout native, mitigasi ReDoS
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.3"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py312"
```

**Baseline v0.3** (9 dependency: fastapi…tenacity) tidak berubah dan tetap final. Sembilan baris di bawahnya adalah **"Pengecualian Sadar"** — dependency yang disetujui owner secara eksplisit, masing-masing dengan rationale tertulis. Default proyek tetap "jangan tambah dependency"; daftar lengkap alasan tiap pengecualian ada di `CLAUDE.md §7` dan diringkas di §Lampiran B dokumen ini. **Jangan menambah dependency baru tanpa melalui proses yang sama** (tanya dulu, catat alasan, catat apa yang ditolak).

**Yang sengaja TIDAK dipakai** (final, jangan diusulkan ulang): SDK resmi Anthropic/OpenAI (raw `httpx` untuk transparansi audit — termasuk Gemini), LangChain/LlamaIndex/framework agent besar, `localStorage`/`sessionStorage` di Web UI.

---

## 6. Struktur Direktori

```
openclawn/
├── core/                     # Otak agent — satu-satunya jalur resmi bicara ke LLM
│   ├── agent_loop.py         # Main loop, tool loop iteratif, policy+approval dual-check
│   ├── llm_client.py         # Ollama + Gemini + Claude, retry + fallback + caching
│   ├── router.py             # Smart routing, soul-aware, multibahasa 3-lapis  [INOVASI 1]
│   ├── router_config.py      # Override peta tier→model (DB-backed, via /router)
│   ├── audit.py              # Routing audit + self-calibration              [INOVASI 1]
│   ├── calibration.py        # RoutingCalibrator + CalibrationStore (loop tertutup)
│   ├── crystallizer.py       # Confidence crystallization, evaluator≥generator [INOVASI 3]
│   ├── compactor.py          # Context compaction (truncation / local / cloud)
│   ├── conversation.py       # Multi-agent conversation (orchestrator + strategi)
│   ├── event_bus.py          # Event-Driven Runtime — event-sourcing ringan
│   ├── guardrails_config.py  # On/off per rail guardrail
│   ├── autopilot.py          # Autopilot terjadwal (AutopilotScheduler)
│   ├── activity.py           # Activity timeline
│   ├── skill_pack.py         # Export/import skill (Skill Marketplace)
│   ├── mcp_client.py         # Klien MCP (stdio + HTTP/SSE)
│   ├── mcp_registry.py       # Registry server MCP + enkripsi env
│   ├── tool_audit.py         # Telemetri penggunaan tool
│   └── prometheus_metrics.py # Exporter metrik Prometheus
│
├── infra/                    # Fondasi — semua modul lain bergantung ke sini
│   ├── config.py             # AppConfig (frozen dataclass, semua angka ajaib)
│   ├── database.py           # DatabaseManager, koneksi shared, migrasi otomatis
│   ├── env.py                 # load_dotenv wrapper
│   ├── logging.py            # structlog JSON + scrub_secrets rekursif
│   ├── backup.py             # Backup/restore SQLite
│   ├── settings.py           # SettingsStore (override runtime via /settings)
│   ├── workspace.py          # Resolusi workspace root per-sesi (path traversal guard)
│   ├── chat_sessions.py      # ChatSessionStore — riwayat chat (tenant-aware)
│   ├── users.py              # UserStore — multi-user + RBAC (tenant-aware)
│   ├── manifest.py           # clawn.yaml parser (Policy Engine deklaratif)
│   └── i18n.py                # String UI ID/EN
│
├── memory/
│   ├── layers.py             # L1-L2-L4 memory management
│   ├── search.py             # FTS5 cross-session search
│   ├── skill_decay.py        # Exponential decay + archival        [INOVASI 2]
│   ├── skill_feedback.py     # Jembatan outcome antar-turn (I2/I3)
│   ├── curator.py            # Skill Curator — konsolidasi (I1)
│   └── user_model.py         # Profil user naratif (I5, opsional)
│
├── roles/
│   ├── contracts.py          # Pydantic output contracts             [INOVASI 4]
│   ├── registry.py           # RoleNegotiator — validasi handoff     [INOVASI 4]
│   ├── pm/soul.toml
│   ├── qa/soul.toml
│   ├── dev/soul.toml
│   ├── data/soul.toml
│   └── security/soul.toml    # read-only murni, tanpa tool tulis/eksekusi/network
│
├── tools/                    # ~28 tool, 13 file, dikelompokkan per kapabilitas
│   ├── __init__.py           # TOOL_REGISTRY
│   ├── base.py                # Tool ABC + requires_approval flag
│   ├── file_ops.py            # file_read, read_many, file_write, file_edit, ...
│   ├── document.py             # pdf_read, doc_write, pdf_write
│   ├── git.py                  # git_status, git_diff, git_log
│   ├── data.py                 # db_query, memory_search, json_query
│   ├── search.py               # glob, grep (regex + timeout)
│   ├── web.py                   # web_fetch, web_search, http_request (SSRF guard)
│   ├── interaction.py           # ask_user
│   ├── todo.py                  # todo_write
│   ├── blocker.py               # report_blocker
│   ├── mcp_tool.py               # mcp__<server>__<tool>
│   ├── code.py                   # code_run → sandbox
│   ├── shell.py                   # shell_run, list_dir
│   ├── workspace_tool.py          # set_workdir
│   └── sandbox.py                  # Docker sandbox runner [keamanan kritis]
│
├── security/
│   ├── vault.py               # Credential injection saat outbound request
│   ├── shield.py               # NFKD normalize + regex (kosmetik, bukan andalan utama)
│   ├── guardrails.py            # GuardrailEngine (ala NeMo) — input/output rail
│   ├── skill_scanner.py          # Scan skill impor (file/URL) dari kode berbahaya
│   ├── approval.py                # ApprovalGate — human-in-the-loop
│   ├── question.py                 # QuestionGate — ask_user interaktif
│   ├── auth.py                      # Self-host auth (shared-secret, cookie session)
│   ├── oidc.py                       # OAuth2/OIDC login
│   ├── rate_limit.py                  # RateLimiter
│   └── policy_engine.py                # PolicyEngine — kondisi tambahan dict/TOML
│
├── web/
│   ├── main.py                # FastAPI app, middleware auth+CSRF, ~50+ endpoint
│   ├── templates/             # HTMX partial templates
│   └── static/                # CSS/JS
│
├── migrations/
│   ├── 001_initial.sql        # Skema utuh (semua tabel inti)
│   └── 002_multi_tenant.sql   # Dokumentasi rencana tenant_id (eksekusi via DatabaseManager)
│
├── tests/                     # ~850+ test, satu file per modul/fitur
├── docs/                      # Referensi teknis per-folder (WAJIB di-update, CLAUDE.md §11)
├── data/                      # SQLite (gitignored)
├── .env.example
├── docker-compose.yml
├── docker-entrypoint.sh       # chown + setpriv privilege-drop
├── Dockerfile.role            # Non-root appuser (uid/gid 1000)
├── Dockerfile.sandbox         # Image untuk code_run
├── Caddyfile.example          # Reverse proxy contoh (self-host)
├── CLAUDE.md                  # Sumber kebenaran "BAGAIMANA"
├── openclawn-core-spec-v0.4.md  # Dokumen ini — sumber kebenaran "APA"
└── pyproject.toml
```

---

## 7. Konfigurasi (`infra/config.py`)

`AppConfig` adalah dataclass **frozen** — semua angka ajaib di satu tempat, bukan tersebar di kode. Field yang paling sering relevan:

| Kategori | Field kunci | Default |
|---|---|---|
| Provider | `ollama_base`, `anthropic_base`, `gemini_base` | localhost / API resmi |
| Auth | `auth_token`, `oidc_issuer`/`oidc_client_id`/`oidc_client_secret`, `session_secret`, `idle_timeout_sec` | kosong = auth OFF (localhost dev) |
| Token budget | `max_context_tokens`, `llm_max_tokens_default`, `llm_max_tokens_with_tools` | 28_000 / 4096 / 8192 |
| Tool loop | `max_tool_hops`, `llm_max_retries`, `approval_timeout_sec` | 5 / 3 / 120s |
| Skill decay (I2) | `decay_interval_sec`, `skill_decay_base`, `skill_archive_threshold`, `skill_revive_boost`, `max_active_skills`, `max_shared_skills` | 3600s / 0.97 / 0.3 / 0.5 / 8 / 3 |
| Crystallizer (I3) | `confidence_threshold` | 4 |
| Memory | `archive_after_turns`, `session_history_turns`, `draft_stale_days` | 6 / 20 / 14 |
| Routing multibahasa | `routing_tech_keywords`, `routing_multistep_keywords`, `routing_urgency_keywords`, `routing_language_bump`, `routing_local_scripts` | ID+EN / OFF / `("latin",)` |
| Compaction | `compaction_default_mode`, `compaction_local_model`, `compaction_keep_recent`, `compaction_min_old_turns` | off / gemma4:e2b / 4 / 3 |
| Fallback | `fallback_chain` | lihat §9 |

Referensi lengkap tiap field (termasuk catatan investigasi bug historis yang menjelaskan *kenapa* satu angka dipilih) ada di `docs/infra.md`. **API key tidak pernah masuk `AppConfig`** — diambil lewat `Vault` saat dibutuhkan (§13).

---

## 8. Database Schema

Sumber kebenaran: `migrations/001_initial.sql` (skema inti) + `migrations/002_multi_tenant.sql` (dokumentasi rencana `tenant_id`, dieksekusi via `DatabaseManager._ensure_columns`/`_rebuild_tables_for_multi_tenant` — migrasi otomatis, idempoten, jalan tiap startup). Referensi kolom-demi-kolom lengkap: `docs/database.md`.

**Inventori tabel** (25 tabel — bertambah dari 8 tabel di v0.3):

| Kelompok | Tabel |
|---|---|
| Memory | `memory_l1`, `memory_l2`, `memory_l4` (FTS5), `session_turns`, `session_workspace`, `chat_sessions` |
| Skills (I2/I3) | `skills`, `skill_versions`, `skill_usage_pending`, `curation_log` |
| Routing (I1) | `routing_events`, `calibration_log` |
| Roles (I4) | `role_handoffs` |
| Approval/HITL | `approval_log` |
| Agent ops | `agent_todos`, `agent_blockers`, `autopilots`, `autopilot_runs`, `tool_invocations`, `crystallization_log` |
| Multi-agent | `conversations` |
| Integrasi | `mcp_servers` |
| Event-Driven Runtime | `agent_events` |
| Identity | `users` |
| Personalisasi (I5) | `user_model` |
| Config | `app_settings` |

**Tabel yang paling sering dirujuk:**

- **`skills`** — inti I2+I3: `status` (`active`/`draft`/`archived`), `confidence`, `generator_model`, `decay_score`, `use_count`, `last_used_at`, `visibility` (`private`/`shared`/`inherited`), `tenant_id`.
- **`routing_events`** — inti I1: 8+ kolom dimensi (`dim_*`), `complexity_label`, `model_chosen`, `had_correction`, `human_feedback`, `evidence_json`, `actor_is_agent` (selalu `1`, pola audit-log standar SIEM).
- **`users`** — identitas + RBAC: `tenant_id`, `subject`, `access_role` (`admin`/`member`/`viewer`), `UNIQUE(tenant_id, subject)`.
- **`agent_events`** — event-sourcing ringan untuk Event-Driven Runtime (Prioritas 4).

**Fungsi kustom SQLite:** `POWER(base, exp)` didaftarkan di `DatabaseManager.conn()` (SQLite tak punya bawaan) — dibutuhkan formula decay eksponensial I2.

---

## 9. Modul Inti — `core/`

### `core/llm_client.py`
Satu-satunya jalur resmi bicara ke LLM. `stream_with_fallback()` adalah satu-satunya entry point publik — tidak ada modul lain yang boleh call LLM langsung. Health-check Ollama sebelum pakai; Anthropic/Gemini diasumsikan up, retry menangani transient error. Retry (`tenacity`) hanya untuk `httpx.HTTPError`. System prompt dibungkus `cache_control: ephemeral` (Claude) untuk prompt caching.

**Fallback chain default:**
```
gemma4:e4b (ollama) → deepseek-r1:latest (ollama) → neural-chat:latest (ollama) → gemini-2.5-flash (gemini)
```

### `core/router.py` — Inovasi 1
Membaca `soul.toml` role sekali saat init (bukan tiap request). `upgrade_keywords` dari soul → +3 skor & **bypass** `prefer_local`. Router **selalu deterministik & tanpa LLM** (routing harus cepat & dapat diaudit).

**Peta tier→model default** (dapat di-override per-instance via `/router`, `RouterConfigStore`):

| Tier | Model | Provider |
|---|---|---|
| TRIVIAL | `gemma4:e4b` | Ollama |
| SIMPLE | `deepseek-r1:latest` | Ollama |
| MODERATE | `qwen3.5:9b` | Ollama |
| COMPLEX | `gemini-2.5-flash` | Gemini |
| CRITICAL | `gemini-2.5-pro` | Gemini |

**Multibahasa, 3 lapis** (detail penuh di `docs/core.md`): (1) sinyal netral-bahasa (panjang query, history) — lantai universal tapi kasar; (2) keyword per-bahasa (`routing_*_keywords`) — tajam tapi perlu diisi per bahasa; (3) sinyal struktural (`_has_code_signal`: code fence, URL, simbol kode) — universal, menutup kelemahan lapis 2. Lapis terpisah: `routing_language_bump` (opt-in) menaikkan tier bila *script* Unicode query di luar `routing_local_scripts`.

### `core/audit.py` — Inovasi 1
`log_decision()` **sebelum** LLM call, `finalize()` **sesudah**. `check_correction()` di awal turn *berikutnya* mendeteksi apakah turn sebelumnya dikoreksi user (via `CORRECTION_SIGNALS`, ID+EN). `calibration_report()` dan `role_report()` (agregasi per role/agent, termasuk `avg_human_feedback` — eksplisit via `set_human_feedback`, beda dari `had_correction` yang implisit) memberi bukti empiris untuk tuning threshold.

### `core/crystallizer.py` — Inovasi 3
**Invariant paling dijaga ketat: evaluator harus minimal setara generator.**

```python
EVALUATOR_FOR = {
    "gemma4:e2b":  ("ollama",    "gemma4:e4b"),
    "gemma4:e4b":  ("ollama",    "gemma4:12b"),
    "gemma4:12b":  ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-sonnet-4-6":         ("anthropic", "claude-sonnet-4-6"),
}
DEFAULT_EVALUATOR = ("anthropic", "claude-haiku-4-5-20251001")
```

> **Catatan status saat ini:** peta di atas mencakup roster Ollama/Claude lama. Roster lokal/cloud yang **aktif dipakai router sekarang** (`deepseek-r1:latest`, `qwen3.5:9b`, `gemini-2.5-flash`, `gemini-2.5-pro`) sebagian besar **tidak ada** di `EVALUATOR_FOR` — ini bukan bug diam-diam: generator yang tak dikenal peta jatuh ke `DEFAULT_EVALUATOR` **dan otomatis ditandai unverified**, sehingga hasilnya di-gate ke `draft` alih-alih diam-diam dianggap "cukup aman" (lihat komentar di `core/crystallizer.py` baris ~159). Ini adalah fail-safe yang disengaja, bukan celah — tapi memperluas `EVALUATOR_FOR` untuk roster saat ini adalah kandidat perbaikan follow-up yang jelas.

Confidence < 4 **atau** ada `critical_gaps` → status `draft`, bukan `active` (draft tidak masuk auto-context). Self-evaluation minta output JSON ketat; parse gagal → fail-safe `confidence=1, critical_gaps=True`. `refine_on_correction()` memperbaiki skill yang dikoreksi user — evaluator ≥ generator menulis ulang, diterapkan HANYA bila `improved && confidence ≥ threshold`; versi lama disimpan ke `skill_versions` (revertible).

### `core/agent_loop.py`
Tool loop **iteratif (`while`)**, bukan rekursif — aturan keras, tak ada pengecualian. `_post_turn` selalu `asyncio.create_task(...).add_done_callback(...)` — tidak ada fire-and-forget tanpa error logging. `soul.toml` di-cache sekali per `AgentLoop` (`_load_soul_once`). Tool schema difilter ke yang diizinkan role sebelum dikirim ke LLM.

`PolicyEngine.evaluate` dicek di **dua titik independen** (§13) — pola defense-in-depth yang sama dipakai `_TRUST_MODE_EXEMPT` (daftar tool yang tak bisa dilewati trust mode meski `bypass_approval=True`).

### `core/event_bus.py` — Event-Driven Runtime (Prioritas 4)
Event-sourcing ringan ke tabel `agent_events`. `Event` dataclass + `EventBus` untuk publish/subscribe internal.

### `core/conversation.py` — Multi-Agent Conversation
`ConversationOrchestrator` + `TurnStrategy` (ABC, beberapa pola giliran) untuk percakapan antar-role, terpisah dari handoff sinkron `RoleNegotiator` (§11).

---

## 10. Modul Memori — `memory/`

### `memory/skill_decay.py` — Inovasi 2

**Formula (eksponensial, bukan linear):**
```
decay_score = decay_score * POWER(0.97, hari_sejak_terakhir_dipakai)
```
Di bawah `skill_archive_threshold` (0.3) → status `archived`. `mark_used()` menaikkan skor (`skill_revive_boost` 0.5) dan mengembalikan status `archived`→`active` (revive). `maybe_run_decay_pass()` di-throttle (`decay_interval_sec`, default 1 jam) — dipanggil tiap turn, mayoritas panggilan no-op.

### `memory/layers.py`
`MemoryManager.load_context()` menyusun L1 (checkpoint key-value), L2 (facts terurut importance), L3 (skill aktif dari `SkillDecayManager`), L4 (FTS5 cross-session — trigger bila query >3 kata **atau** mengandung `SPECIFIC_TERMS`).

### `memory/curator.py`, `memory/skill_feedback.py`, `memory/user_model.py` — Compounding Intelligence
`SkillCuratorManager` (I1) mengonsolidasi skill serupa/duplikat berdasarkan ambang pemakaian → `curation_log`. `SkillFeedback` menjembatani outcome satu turn ke turn berikutnya (menutup celah I2/I3 — skill yang baru dipakai belum tentu langsung diketahui hasilnya). `UserModel` (I5, opsional) membangun profil user naratif dari histori interaksi.

---

## 11. Peran & Kontrak — `roles/`

### `roles/contracts.py` — Inovasi 4
5 kontrak Pydantic: `PMOutput`, `QAOutput`, `DevOutput`, `DataOutput`, `SecurityOutput` — terdaftar di `CONTRACT_REGISTRY`. Output tidak valid → `RoleNegotiator._validate` tidak crash: `validation_ok=0` disimpan bersama raw output ke `role_handoffs` untuk debugging.

### 5 Role Aktif

| Role | Karakter akses |
|---|---|
| **PM** | Dokumen (PRD dsb.); query dokumen di-*upgrade* tier via keyword khusus. |
| **QA** | Bisa simpan test case sebagai Word/Excel/PDF; `shell_run` & `code_run` diizinkan. |
| **Dev** | Akses tool paling luas, termasuk edit/patch file & `code_run` (selalu approval). |
| **Data** | Query database read-only, `code_run` diizinkan, TAPI tidak `shell_run`/`http_request`. |
| **Security** | **Read-only mutlak by design** — tanpa tool tulis/eksekusi/network, termasuk wildcard MCP (`mcp__*`) ditolak eksplisit di test (mencegah regresi diam-diam). |

Tiap role: `soul.toml` berisi `[system_prompt]`, `[tools] allowed = [...]`, `[routing]` (`prefer_local`, `upgrade_keywords`, keyword multibahasa tambahan), dan opsional `[policy.<tool_name>]` (§13).

---

## 12. Tools + Sandbox — `tools/`

Setiap `Tool` (ABC di `tools/base.py`) punya `requires_approval: bool`. Permission dikontrol via `soul.toml[tools][allowed]` per role — bukan hardcoded di kode tool. Semua tool filesystem dibatasi ke `workspace_root` resolusi (`infra/workspace.py`, guard path traversal).

**Permission matrix ringkas** (matrix lengkap: `docs/tools.md`):

| Kelompok tool | Butuh approval? |
|---|---|
| `file_read`, `read_many`, `list_dir`, `set_workdir`, `glob`, `grep`, `pdf_read`, `memory_search`, `json_query`, `ask_user`, `todo_write`, `report_blocker`, `git_*` | Tidak |
| `file_write`, `file_edit`, `file_append`, `apply_patch`, `doc_write`, `pdf_write`, `http_request`, `db_query` | **Ya** |
| `shell_run` | Tidak — sudah *sandboxed* (v0.11.0, trust mode) |
| `code_run` | **Ya, selalu, tanpa pengecualian** |

### `tools/sandbox.py` — DockerSandbox

Flag keamanan wajib pada **setiap** invocation `docker run` (satu sumber kebenaran, dites via capture argv sungguhan, bukan cuma mock):

```python
SANDBOX_IMAGE = "openclawn-sandbox:latest"
SANDBOX_TIMEOUT_SEC = 30
SANDBOX_MEM_LIMIT = "256m"
SANDBOX_CPU_LIMIT = "0.5"

# _MANDATORY_SECURITY_FLAGS (CLAUDE.md §1.1):
("--network", "none")   # isolasi network total
("--read-only",)        # root filesystem read-only
("--user", "nobody")    # non-root
# + --memory, --cpus, --tmpfs (writable ephemeral), --security-opt no-new-privileges
```

Tidak ada `exec()`, `eval()`, atau `subprocess` langsung ke host untuk kode dari LLM — **hanya** lewat `DockerSandbox`.

---

## 13. Security Layer — `security/` + `infra/users.py`

Filosofi: **isolasi container adalah pertahanan utama; segala sesuatu di luar itu adalah defense-in-depth, bukan pengganti.** `Shield` secara sadar didokumentasikan sebagai lapisan kosmetik.

| Komponen | Melindungi dari |
|---|---|
| `security/vault.py` | Credential bocor ke prompt/log/DB. `encrypt_secret`/`decrypt_secret` (Fernet) untuk `mcp_servers.env` (satu-satunya jalur credential yang perlu tersimpan di tabel — dienkripsi, bukan plaintext). |
| `security/guardrails.py` (`GuardrailEngine`) | Input rail (prompt injection) & output rail (kebocoran system-prompt, PII) — ala NVIDIA NeMo Guardrails; on/off per-rail via `core/guardrails_config.py`. |
| `security/shield.py` | Prompt injection kasar, NFKD normalize (cegah homoglyph) — kosmetik, dipakai oleh input rail. |
| `security/skill_scanner.py` | Skill impor (file/URL) membawa kode berbahaya/eksfiltrasi. |
| `security/approval.py` (`ApprovalGate`) | Aksi destruktif tanpa persetujuan eksplisit — timeout → **deny** (fail-safe). |
| `security/policy_engine.py` (`PolicyEngine`) | Kondisi tambahan **di atas** allow-list statis (`soul.toml`) dan `requires_approval` statis — TIDAK menggantikan keduanya. |
| `security/auth.py` | Self-host shared-secret login, cookie session HMAC-signed. |
| `security/oidc.py` | OAuth2/OIDC login — mode TAMBAHAN, bukan pengganti shared-secret. |
| `infra/users.py` (`UserStore`) | Multi-user + RBAC. |
| `security/rate_limit.py` | Brute-force / abuse endpoint publik. |
| `security/question.py` (`QuestionGate`) | Tool `ask_user` interaktif — sinkron dengan Web UI. |

### `PolicyEngine` — kondisi berbasis dict/TOML, sengaja BUKAN DSL/`eval()`

```toml
[policy.file_write]
deny_if = [{ field = "path", op = "prefix", value = "/etc" }]

[policy.http_request]
approval_required_if = [{ field = "url", op = "not_prefix", value = "https://api.internal" }]
```

`deny_if` **selalu menang** atas `approval_required_if` bila keduanya match (fail-safe: penolakan > permintaan approval). Dievaluasi di **dua titik independen** di `agent_loop.py` — sebelum status UI di-emit, dan sebelum eksekusi tool — supaya bug di satu titik tak membuka celah bypass. Trust mode (`bypass_approval`) dipaksa `False` bila policy memaksa approval: policy adalah lapisan yang lebih kuat daripada preferensi otonomi sesi.

### RBAC (`infra/users.py`)

`users` table: `access_role` (`admin`/`member`/`viewer`) — **beda** dari `role` fungsional (pm/qa/dev/data/security = persona agent, tabel lain). Hierarki: `viewer < member < admin`. Endpoint config sistem (`/settings`, `/skills/import`, `/mcp/*`, `/router`, `/autopilots/delete`, `/admin/users`) gated `_require_role(request, "admin")`. Chat & lihat skills/metrics/conversations tetap terbuka untuk semua role login. **RBAC di-skip total bila auth nonaktif** (`CONFIG.auth_active == False`, default localhost dev) — tak mengubah perilaku deployment lama.

Bootstrap admin: user OIDC pertama per tenant → `admin` otomatis. Shared-secret login selalu `admin` (satu-satunya user shared-secret per tenant).

---

## 14. Multi-Tenant

Kolom `tenant_id` (default `'default'`, kompatibilitas mundur penuh) ditambahkan ke 6 tabel: `memory_l1`, `memory_l2`, `chat_sessions`, `skills`, `routing_events`, `approval_log`, plus `users`.

**Dua kategori sadar:**

| Kategori | Tabel | Status wiring |
|---|---|---|
| **Wired penuh** (bukti konsep) | `chat_sessions`, `skills` | `ChatSessionStore` & `SkillDecayManager` menerima `tenant_id` di constructor dan memfilter SEMUA query — termasuk operasi tunggal-ID (defense-in-depth). |
| **Kolom pasif** | `memory_l1`, `memory_l2`, `routing_events`, `approval_log` | Kolom ada, query belum difilter per-tenant — perilaku lama tetap jalan; wiring penuh adalah follow-up terpisah. |

Migrasi skema untuk DB existing (`DatabaseManager`) jalan otomatis tiap startup, idempoten via `PRAGMA table_info`. Tabel dengan constraint `UNIQUE` yang berubah (`memory_l1`, `skills`) di-*rebuild* penuh (create→insert-select→drop→rename); tabel lain cukup `ALTER TABLE ADD COLUMN`.

SQLite tetap default (data sovereignty self-hosted). Jalur ke PostgreSQL untuk skala lintas-proses didokumentasikan penuh di `docs/postgres-migration.md` — perubahan driver, bukan perubahan skema logis.

---

## 15. Web UI — `web/`

FastAPI + HTMX + SSE streaming. Middleware `auth_and_csrf_middleware` menangani kedua mode auth (shared-secret + OIDC) dan RBAC gate. ~50+ endpoint dikelompokkan: chat/streaming, `/metrics` (dashboard kalibrasi I1 + `/metrics/roles` JSON), `/skills` (lihat/impor/export skill pack), `/router` (override peta tier→model, admin-only), `/settings` (admin-only), `/conversations` (multi-agent), `/admin/users` (RBAC, admin-only), `/mcp/*` (registry server MCP, admin-only), `/evidence/{event_id}` (Immutable Audit Evidence, §2), `/feedback/{event_id}` (human feedback eksplisit).

Endpoint & template lengkap: `docs/web.md`.

---

## 16. Testing Strategy

- Framework: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`).
- DB selalu `:memory:` — jangan pernah sentuh `data/openclawn.db` asli.
- LLM **selalu** di-mock (`unittest.mock.AsyncMock`) — test tidak boleh memanggil Ollama/Gemini/Claude sungguhan.
- ~850+ test, satu file per modul/fitur (lihat inventori penuh di `docs/tests.md`).
- Test wajib minimum (tak boleh hilang): router soul-upgrade menaikkan kompleksitas & `prefer_local` menahan di Ollama; fallback Ollama-down→turun chain; crystallizer evaluator tak lebih lemah dari generator (termasuk fail-safe generator-tak-dikenal); skill decay memudar & revive; contracts valid lolos/tak valid ditolak tanpa crash; RBAC bootstrap-admin/member-forbidden/promote; migrasi multi-tenant idempoten & isolasi tenant.

Definition of Done per komponen ada di `CLAUDE.md §4`.

---

## 17. Roadmap & Riwayat Rilis

### Sprint 0–4 (v0.3, fondasi) — ✅ selesai 2026-06-15
`infra/` → `llm_client` (retry+fallback) → `agent_loop` (iteratif) → Web UI streaming → 4 inovasi inti versi awal → sandbox Docker → HITL approval → test coverage dasar.

### Riwayat versi (v0.3.0-alpha → v0.12.0)

| Versi | Tanggal | Sorotan |
|---|---|---|
| `0.3.0-alpha` | 2026-06-19 | 4 inovasi inti, multi-agent conversation, hybrid LLM+routing, 25 tool, security dasar |
| `0.4.0-alpha` | 2026-06-20 | Routing multibahasa 3-lapis, **Compounding Intelligence** (Sprint 6–8) |
| `0.5.0-alpha` | 2026-06-21 | Skill scanner, context compaction "headroom", klien MCP, polish single-user |
| `0.6.0-alpha` | 2026-06-22 | UI enhancement + audit pasca-merge |
| `0.7.0` | 2026-06-17 | Guardrails ala NeMo, fix FTS5 sanitization |
| `0.8.0` | 2026-07-01 | Gated Skill Curator (I1 completion), i18n UI (EN/ID), fix migrasi kolom |
| `0.9.0` | 2026-07-01 | Self-host production hardening — auth opt-in, CSRF, rate limiting |
| `0.10.0` | 2026-07-02 | Working directory adaptif per-sesi, riwayat percakapan per-sesi, heartbeat SSE |
| `0.11.0` | 2026-07-02 | Trust mode (`shell_run` tanpa approval), pindah folder kerja lewat chat, riwayat chat sidebar |
| **`0.12.0`** | **2026-08-02** | **Audit production-readiness** (IDOR chat/approval, XSS, RBAC gap), **Multi-Tenant & RBAC**, **Policy Engine + `clawn.yaml`**, **Event-Driven Runtime**, Governance/Audit Trail (evidence, human feedback), Ecosystem (Prometheus, skill marketplace, OpenConnector), enkripsi-at-rest, mitigasi ReDoS |

Detail lengkap tiap rilis: `CHANGELOG.md`. Detail temuan & perbaikan audit v0.12.0 (bug nyata yang ditemukan & ditutup, bukan asumsi): `TODO.md`.

### Belum dikerjakan / sengaja ditunda

- Tuning threshold router dari data audit **nyata** (bukan data seed) — tooling (`RoutingCalibrator`, `scripts/seed_routing.py`, `scripts/route_sensitivity.py`) sudah siap, keputusan menunggu traffic produksi.
- Ekstraksi 4 inovasi inti jadi paket standalone — backlog refactor struktural, bukan mendesak.
- `EVALUATOR_FOR` (§9) belum mencakup roster model lokal/cloud terbaru (`deepseek-r1`, `qwen3.5`, `gemini-2.5-*`) — fail-safe sudah menangani dengan aman (jatuh ke `draft`), tapi memperluas peta adalah perbaikan follow-up yang jelas.
- Wiring penuh `tenant_id` untuk `memory_l1`/`memory_l2`/`routing_events`/`approval_log` (§14) — kolom sudah ada, filter query belum.
- Scaling horizontal / migrasi Postgres — belum ada kebutuhan pilot nyata yang memvalidasinya.

---

## 18. Quick Start

```bash
git clone <repo> openclawn && cd openclawn
python -m venv .venv && source .venv/bin/activate   # Python 3.12+ wajib
pip install -e ".[dev]"

# Migrasi jalan otomatis saat DatabaseManager pertama kali connect,
# tapi bisa juga manual untuk instalasi baru:
mkdir -p data
sqlite3 data/openclawn.db < migrations/001_initial.sql

cp .env.example .env          # isi ANTHROPIC_API_KEY / GOOGLE_API_KEY sesuai kebutuhan

# Build sandbox image untuk code_run (WAJIB sebelum tool ini dipakai)
docker build -t openclawn-sandbox:latest -f Dockerfile.sandbox .

# Ollama (tier lokal)
ollama pull gemma4:e4b && ollama pull deepseek-r1:latest && ollama pull qwen3.5:9b

uvicorn web.main:app --reload --port 8000
# Chat:    http://localhost:8000
# Metrics: http://localhost:8000/metrics
```

**Tanpa Python 3.12 di mesin lokal** (mis. masih Python 3.9): jalankan lewat Docker image resmi, cara ini juga yang dipakai CI:
```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim bash -lc \
  "pip install --quiet uv && uv sync --frozen --extra dev --quiet && uv run pytest -q"
```

### Verifikasi 4 inovasi + fitur inti

```bash
# Inovasi 1 — audit + kalibrasi + evidence
sqlite3 data/openclawn.db "SELECT complexity_label, dim_soul_upgrade_hit, had_correction, human_feedback FROM routing_events LIMIT 5;"

# Inovasi 2 — exponential decay
sqlite3 data/openclawn.db "SELECT skill_name, status, ROUND(decay_score,3) FROM skills ORDER BY decay_score DESC;"

# Inovasi 3 — confidence + generator/evaluator tracking
sqlite3 data/openclawn.db "SELECT skill_name, status, confidence, generator_model FROM skills;"

# Inovasi 4 — role handoffs
sqlite3 data/openclawn.db "SELECT from_role, to_role, validation_ok FROM role_handoffs;"

# RBAC — multi-user
sqlite3 data/openclawn.db "SELECT tenant_id, subject, access_role FROM users;"

# Event-Driven Runtime
sqlite3 data/openclawn.db "SELECT event_type, created_at FROM agent_events ORDER BY id DESC LIMIT 5;"
```

---

## Lampiran A: Audit Resolution v0.3 (historis)

Status setiap poin dari audit eksternal 2026-06-15 yang mendasari v0.3 — dipertahankan sebagai catatan sejarah, semuanya masih valid di kode saat ini kecuali dicatat lain:

| # | Isu audit | Status |
|---|---|---|
| 1 | Router abaikan soul.toml | ✅ Diambil — tetap berlaku |
| 2 | Keyword classifier rapuh | 🔶 Ditunda — evolusi jadi 3-lapis multibahasa (§9) |
| 3 | `_post_turn` fire-and-forget | ✅ Diambil — tetap berlaku |
| 4 | Evaluator crystallization circular | ✅ Diambil — lihat catatan drift `EVALUATOR_FOR` di §9 |
| 5 | Tidak ada fallback model | ✅ Diambil — tetap berlaku |
| 6 | Decay linear → exponential | ✅ Diambil — tetap berlaku |
| 7 | Decay pass terlalu sering | ✅ Diambil — tetap berlaku |
| 8 | DB_PATH hardcoded | ✅ Diambil — tetap berlaku |
| 9 | Koneksi DB per metode | ✅ Diambil — tetap berlaku |
| 10 | Tool loop rekursif | ✅ Diambil — tetap berlaku |
| — | Retry logic, sandbox, prompt caching, HITL, structured logging, FTS5 threshold | ✅ Diambil — tetap berlaku |
| nit5 | Nama model Claude "salah" | ❌ Ditolak — sudah diverifikasi valid |

Untuk temuan audit produksi v0.12.0 (jauh lebih besar cakupannya: IDOR, XSS, RBAC gap, ReDoS, enkripsi-at-rest), lihat `TODO.md` dan `CHANGELOG.md § [0.12.0]`.

---

## Lampiran B: Pengecualian Sadar Dependency

Ringkas — rationale penuh tiap baris ada di `CLAUDE.md §7`, jangan diduplikasi di sini secara verbatim (satu sumber kebenaran):

| Dependency | Alasan singkat | Ditolak sebagai alternatif |
|---|---|---|
| `pypdf`, `python-docx`, `python-pptx`, `openpyxl`, `reportlab` | Baca/tulis dokumen, murni-Python tanpa dependency sistem | — |
| `mcp` | SDK resmi protokol terbuka (bukan SDK vendor-LLM) untuk ekosistem tool eksternal | Raw JSON-RPC (cakupan tidak penuh: resources/prompts/SSE/OAuth) |
| `pyyaml` | Writer YAML untuk `clawn.yaml` — `tomllib` bawaan hanya baca | Serializer TOML manual |
| `authlib` | OAuth2/OIDC, httpx-based, JWKS via `joserfc` | Implementasi JWT manual sendiri |
| `cryptography` (Fernet) | Enkripsi-at-rest `mcp_servers.env` | Implementasi AES manual sendiri |
| `regex` | Timeout native untuk GrepTool — mitigasi ReDoS | `asyncio.wait_for` (tak bisa membatalkan regex CPU-bound) |

**Pola konsisten:** library teraudit selalu menang atas implementasi kripto/keamanan buatan sendiri. Dependency baru di luar daftar ini butuh persetujuan eksplisit dengan alasan tertulis yang sama — bukan inisiatif agent sepihak.

---

## Lampiran C: Peta Cepat "Mau Kerjakan Apa"

| Mau kerjakan | Baca spec bagian | Baca docs/ | File utama |
|---|---|---|---|
| Config & DB | §7–8 | `infra.md`, `database.md` | `infra/config.py`, `infra/database.py` |
| LLM + fallback | §9 | `core.md` | `core/llm_client.py` |
| Router (I1) | §9 | `core.md` | `core/router.py`, `core/router_config.py` |
| Audit + kalibrasi (I1) | §9 | `core.md` | `core/audit.py`, `core/calibration.py` |
| Skill decay (I2) | §10 | `memory.md` | `memory/skill_decay.py` |
| Crystallizer (I3) | §9 | `core.md` | `core/crystallizer.py` |
| Contracts (I4) | §11 | `roles.md` | `roles/contracts.py`, `roles/registry.py` |
| Tools + sandbox | §12 | `tools.md` | `tools/*.py` |
| Security + RBAC + Policy Engine | §13 | `security.md` | `security/*.py`, `infra/users.py` |
| Multi-tenant | §14 | `database.md` | `infra/database.py` (migrasi) |
| Web UI | §15 | `web.md` | `web/main.py`, `web/templates/*` |
| Role config | §11 | `roles.md` | `roles/{pm,qa,dev,data,security}/soul.toml` |
| Test | §16 | `tests.md` | `tests/*.py` |
| Migrasi ke Postgres | §14 | `postgres-migration.md` | — |

---

*Living spec — update setiap ada perubahan arsitektur signifikan (bukan tiap commit; perubahan detail publik cukup di `docs/*.md` per `CLAUDE.md §11`). Selaras dengan `CLAUDE.md`; keduanya di-update bersamaan.*
