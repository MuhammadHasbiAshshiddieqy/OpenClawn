# Changelog

All notable changes to OpenCLAWN are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/) with pre-release suffixes during the research phase.

## [Unreleased]

### Added — single-user production polish
Setelah review pihak ketiga (yang sebagian keliru menilai OpenCLAWN sebagai produk
multi-user), dua gap yang BENAR-BENAR valid untuk self-hosted single-user ditutup:
- **`/health` endpoint** — JSON status + cek konektivitas DB, untuk monitoring self-hosted.
- **Stale-draft cleanup** — draft yang tua (`draft_stale_days`, default 14) & tak pernah
  terbukti (`draft_success_count=0`) diarsipkan saat decay pass — cegah menumpuk. ARSIP,
  bukan hapus (konsisten "tak ada kehilangan data senyap"). `draft_stale_days=0` → nonaktif.
- README: section **Scope & Production Posture** — menegaskan auth/Postgres/scaling DI LUAR
  scope secara SADAR (§7 single-user), bukan utang teknis. +5 test.

### Added — MCP (Model Context Protocol) client
OpenCLAWN kini dapat memakai tool dari **server MCP eksternal** (GitHub, filesystem,
dll) — sebelumnya hanya 26 tool bawaan. Via SDK resmi `mcp` (CLAUDE.md §7: MCP bukan
SDK vendor-LLM, jadi tak melanggar transparansi jalur LLM).
- **Transport:** stdio (subprocess lokal) + HTTP streamable (remote).
- **Keamanan (§1):** tool MCP **SELALU butuh approval** (HITL; di autopilot jadi
  proposal) — server eksternal tak terkendali. Remote di-guard SSRF sebelum konek.
  Role harus **opt-in** via `soul.toml` wildcard `mcp__*` / `mcp__<server>__*`.
- Tool MCP tak dapat jalur istimewa: dibungkus `MCPTool` (subclass `Tool`), lewat
  pagar yang sama (izin/validasi/telemetri/timeout). Discover fail-safe (server error
  di-skip, tak jatuhkan startup). Kelola via halaman `/mcp` (tabel `mcp_servers`).
- `core/mcp_client.py` · `core/mcp_registry.py` · `tools/mcp_tool.py`. +19 test (420→439).

## [0.4.0-alpha] — 2026-06-20

Pre-release kedua. Membangun di atas 4 inovasi inti: multi-agent conversation matang,
**autopilots** terjadwal (proposal-gated), **skill packs** (export/import), **compounding
intelligence** (skill yang merapikan & memperbaiki dirinya), **activity timeline**,
**blocker reporting**, **routing multibahasa**, dan pengerasan keamanan (SSRF guard).
Masih fase research — single-user, API dapat berubah, test memakai mock LLM.

### Changed — routing multibahasa (§1.5), tiga lapis sinyal
Routing kini menangani dua masalah multibahasa secara eksplisit (deterministik, tanpa LLM):
- **Keyword config+soul (§1.5):** keyword tech/multistep/urgency tak lagi hardcoded di
  core → `AppConfig.routing_*_keywords` (default ID+EN) + ekstra per role via
  `soul.toml [routing]`. Memperbaiki pelanggaran §1.5.
- **Sinyal struktural bahasa-agnostik** (`has_code_signal`): deteksi code fence/URL/
  simbol kode → query teknis pendek dalam bahasa APA PUN naik tier tanpa keyword.
- **Language bump (Masalah B, opt-in `routing_language_bump`, default OFF):** deteksi
  script query via Unicode (`_detect_script`); bila di luar `routing_local_scripts`
  (default `latin`), naikkan tier ke model cloud yang umumnya lebih multibahasa —
  menjawab "model belum tentu kuat di semua bahasa".
- Didokumentasikan lengkap di `docs/core.md` (cara kerja, keterbatasan jujur, cara
  perluas). +12 test (router 16→28, total 408→420).

### Added — Compounding Intelligence (Sprint 6–8, Hermes-parity)
Library skill kini **merapikan & memperbaiki dirinya** seiring pemakaian — efek
compounding, tetap dalam rem: gated, versioned, revertible, auditable.
- **Prasyarat — revive ter-wire:** `mark_used` dulu ada tapi tak pernah dipanggil
  (revive dorman); kini di-wire via `SkillFeedback` + tabel `skill_usage_pending`.
- **I1 Skill Curator** (`memory/curator.py`) — gabung/dedup skill mirip (pre-filter
  Jaccard → LLM judge gated ≥4). Loser jadi `merged` (tak dihapus), revertible.
  Panel "Curation" + tombol Batalkan di `/skills`; jejak di `curation_log`.
- **I2 Draft auto-promotion** — draft yang dipakai-sukses N× naik `active`; dikoreksi
  → reset. Draft dapat 1 slot percobaan di `get_active_skills`.
- **I3 Skill refine-on-correction** — skill yang menyesatkan (turn-nya dikoreksi)
  ditulis ulang oleh evaluator ≥ generator, hanya bila confident; versi lama di
  `skill_versions` (revertible).
- **I4 Guarded auto-apply** kalibrasi routing — opt-in (`calibration_auto_apply`,
  default OFF §8), throttled, clamp ±1, `source='auto'`, tetap revertible. Badge
  `auto-tune` di `/metrics`.
- **I5 Dialectic user model** (opsional, `memory/user_model.py`) — profil user naratif
  lintas sesi, default OFF, versioned, dapat dihapus (privasi §1).

Semua pass throttled & post-turn (tak memblokir turn). +35 test (373→408).

### Added — fitur terinspirasi Multica (multica-ai/multica)
- **Activity Timeline** (`/activity`) — linimasa kronologis aksi agent lintas tabel
  (routing · tool · handoff · conversation · crystallize · blocker), filter per role.
  Agregasi read-only, tanpa tabel baru (`core/activity.py`).
- **Proactive blocker reporting** — tool `report_blocker` (tool ke-26): agent menandai
  hambatan secara terstruktur & asinkron (beda dari `ask_user` yang memblokir). Tampil
  menonjol di `/activity`, bisa ditutup user (`agent_blockers`).
- **Autopilots** (`/autopilots`) — tugas agent terjadwal (scheduler asyncio in-process,
  tanpa dependency baru). **Aman by design (§1, §17):** berjalan read-only; aksi
  butuh-approval TIDAK dieksekusi otomatis — diantri sebagai *proposal* untuk ditinjau
  user (`AgentConfig.autopilot`, `ApprovalGate.queue_proposal`). Misfire-safe.
- **Skill packs** (`/skills/export`, `/skills/import`) — berbagi skill antar-instalasi
  sebagai berkas Markdown (terinspirasi `skills-lock.json` Multica). **Impor berlapis
  keamanan (§1):** SSRF guard (URL) → Shield scan (anti prompt-injection) → status
  `draft` (tak auto-masuk context, user aktifkan manual) → hash SHA-256 (integritas,
  dicatat di `skills-lock.json`). `core/skill_pack.py`.

### Security
- SSRF guard pada `web_fetch`/`http_request` (rilis sebelumnya, dipertahankan).
- Autopilot tidak pernah mengeksekusi aksi destruktif tanpa persetujuan eksplisit —
  HITL tetap utuh meski agent berjalan tanpa manusia di depan.

## [0.3.0-alpha] — 2026-06-19

First tagged pre-release. Feature-complete against the v0.3 core spec, but still in
the **research/experiment phase**: single-user, no auth, APIs may shift before a
stable `0.3.0`. All tests mock the LLM/Docker/Ollama layers — expect to validate
your own local setup end-to-end.

### The 4 core innovations
- **Routing audit + self-calibration** — every routing decision is logged with its
  8 dimensions and later checked for correction; `/metrics` surfaces tuning
  recommendations you apply (or revert) by hand.
- **Skill decay** — skills fade exponentially (`0.97 ^ days_idle`) and archive below
  threshold; reuse revives them.
- **Confidence-gated crystallization** — the agent self-evaluates a solution before
  storing it as a skill; an evaluator no weaker than the generator gates it.
- **Role output contracts** — typed Pydantic handoffs (PM/Dev/QA/Data/Security);
  invalid output degrades gracefully (`validation_ok=0`) instead of crashing.

### Multi-agent conversation
- Three patterns over one orchestrator: **pipeline** (sequential handoff), **debate**
  (round-robin N rounds), **orchestrator** (lead delegates dynamically with a fixed
  fallback). Live **stop** and **interject** mid-conversation. Transcripts archived
  to `/conversations`.

### Hybrid LLM + routing
- Local-first fallback chain (Ollama → Gemini) with graceful degradation; raw httpx,
  no vendor SDKs (audit transparency). Editable tier→model map via `/router`; manual
  model override via `/settings`.

### Tooling (25 tools)
- Files (read/write/edit/patch/glob/grep, batch `read_many`), code & shell **only**
  inside a Docker sandbox (`--network none`, `--read-only`, non-root,
  `no-new-privileges`), documents (`doc_write` docx/pptx/xlsx/md, `pdf_write`,
  `pdf_read`), git (read-only status/diff/log via sandbox), `todo_write`, web
  (`web_fetch`/`web_search`/`http_request`).

### Security
- `code_run`/`shell_run` never execute on the host — Docker sandbox only.
- Credentials injected from `Vault` on outbound requests only; never in
  context/prompt/DB, and scrubbed from logs.
- **Anti-SSRF guard** on `web_fetch`/`http_request`: rejects loopback, RFC1918, and
  link-local hosts (incl. cloud metadata `169.254.169.254`), resolving DNS to catch
  rebinding.
- Central tool safety net: schema validation + timeout + graceful error + telemetry.

### Observability & ops
- `/metrics` (routing calibration + tool telemetry), `/skills` (decay curves +
  crystallization log), `/conversations` (multi-agent archive).
- GitHub Actions CI (ruff format + lint + pytest) with an enforced `uv.lock` for
  reproducible builds.

### Quality
- **325 tests** passing, `ruff` clean.
