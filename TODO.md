# TODO.md — OpenCLAWN Roadmap & Backlog

> **Catatan rekonstruksi (2026-07-27):** `TODO.md`, `KESIMPULAN.md`, dan
> `PRODUCTION-READINESS.md` yang dirujuk berulang kali di riwayat commit
> (`git log`) ternyata tidak pernah masuk git — bukan terhapus, memang
> tidak ada satu pun commit yang menambahkannya. Berarti ketiganya adalah
> dokumen kerja yang hidup di luar repo (device lokal/tool eksternal) dan
> saat ini tidak bisa dipulihkan begitu saja. File ini menyusun ulang
> status Prioritas 1–6 murni dari pesan commit + audit kode langsung
> (bukan tebakan), lalu menambahkan validasi tren pasar 2026 yang dicari
> ulang hari ini (bukan mengutip angka lama yang sumbernya sudah hilang).
> Mulai sekarang **file ini di-commit ke git** — supaya tidak hilang lagi.

---

## 1. Status Prioritas (rekonstruksi dari commit history)

### Prioritas 1 & 1.5 — Production-Readiness Blockers — ✅ SELESAI (7/7 + follow-up)
- Evidence-based response (`GET /evidence/{event_id}`), Human Approval Pipeline
  sebagai node query-able (`approval_log.approval_id`), Runtime Evaluation
  Engine (per-role KPI + human feedback), format audit log standar pasar
  (`user_id`, `actor_is_agent`).
- SQLite backup/restore (Online Backup API) + session idle timeout.

### Prioritas 2 — Governance & Audit Trail — ✅ SELESAI (4/4)
- Evidence snapshot per turn, approval sebagai entitas traceable, role-level
  KPI (`role_report`), kolom `user_id`/`actor_is_agent` di `routing_events`
  dan `tool_invocations`.

### Prioritas 3 — Policy Engine — ✅ SELESAI
- `security/policy_engine.py` + `clawn.yaml`: lapisan kondisi tambahan
  (deny_if / approval_required_if) di atas allow-list statis dan
  `requires_approval`, dievaluasi di dua titik (defense-in-depth).

### Prioritas 4 — Event-Driven Runtime — ✅ SELESAI
- `core/event_bus.py`: pub/sub in-process murni asyncio, tanpa broker
  eksternal. `conversation.py` di-refactor jadi publisher/subscriber
  granular per event token/thinking/status/usage.

### Prioritas 5 — Multi-Tenant & Enterprise Identity — ✅ SELESAI (4/4)
- `tenant_id` di 6 tabel (2 di-rebuild penuh untuk UNIQUE constraint),
  OAuth2/OIDC login (opsional, di samping shared-secret), multi-user RBAC
  sungguhan (tabel `users`, role admin/member/viewer, `_require_role`),
  dokumentasi jalur migrasi SQLite→PostgreSQL.

### Prioritas 6 — Ecosystem & Ops — ✅ SELESAI
- Prometheus metrics endpoint (`/metrics/prometheus`, 8 metric family,
  hand-written exposition format), cross-role skill marketplace
  (private/shared/inherited visibility), integrasi opsional OpenConnector
  (external MCP server, Apache-2.0, opt-in Docker service) + akses granular
  per role.

**Kesimpulan:** semua 6 prioritas yang tercatat di commit history sudah
tuntas. Tidak ada item "Prioritas 1–6" yang menggantung.

---

## 2. Bug/gap ditemukan — audit produksi 2026-07-27

Audit kode langsung (bukan re-baca dokumentasi) menemukan 2 blocker baru
yang **belum pernah masuk TODO manapun** — regresi laten dari ekspansi
router multi-provider yang tidak diikuti update ke modul lain. Status per
2026-07-27/28 (sesi lanjutan): **semua item — 2 blocker + 3 should-fix + 1
nice-to-have — sudah diperbaiki dan terverifikasi hijau** (804 test lulus,
`ruff check`/`ruff format --check` bersih via Docker `python:3.12-slim` +
`uv sync --frozen`, plus CI GitHub Actions sungguhan hijau untuk commit
`13ac46f`; lihat §"Verifikasi CI sungguhan" di bawah).

### 🔴 Blocker
- [x] **`core/crystallizer.py` — `EVALUATOR_FOR` tidak sinkron dengan
  `router.py`.** ~~Router sekarang punya tier `gemini-2.5-flash` /
  `gemini-2.5-pro` / `deepseek-r1` / `qwen3.5:9b`, tapi `EVALUATOR_FOR`
  masih mengacu roster lama~~ → **DIPERBAIKI**: `EVALUATOR_FOR` diperluas
  mencakup roster aktif (`deepseek-r1`→Haiku, `qwen3.5:9b`→Haiku,
  `gemini-2.5-flash`→`gemini-2.5-pro`, `gemini-2.5-pro`→`claude-sonnet-4-6`).
  Lebih penting: `_resolve_evaluator()` baru menandai `verified=False` untuk
  generator model APAPUN di luar peta (bukan cuma roster hari ini — proteksi
  ke depan) dan `crystallize()`/`refine_on_correction()` memaksa
  `draft`/`skipped` saat `verified=False`, jadi drift roster di masa depan
  gagal AMAN (draft) alih-alih diam-diam lolos jadi `active` via
  `DEFAULT_EVALUATOR`. Test regresi: `test_evaluator_map_covers_router_roster`,
  `test_unverified_generator_forces_draft_even_high_confidence`
  (`tests/test_crystallizer.py`). Bonus: `router.py::_explain()` juga
  memperbaiki drift serupa (reason string audit trail sebelumnya
  hardcode "Claude Haiku"/"Claude Sonnet" walau tier itu sudah pindah ke
  Gemini — sekarang ambil dari `model_map` aktif).
- [x] **`core/llm_client.py` — retry pada `_stream_one` tidak pernah
  jalan.** ~~`@retry` tenacity membungkus async generator~~ → **DIPERBAIKI**:
  retry ditulis manual (loop + `asyncio.sleep` exponential, tanpa
  dependency baru) di `_stream_one`, memanggil `_stream_one_attempt` (dulu
  isi `_stream_one`) di dalamnya. Retry HANYA sebelum chunk pertama
  ter-yield ke caller — kegagalan setelah itu propagate langsung (tak
  di-retry) supaya tak menduplikasi output yang sudah terlanjur dikirim ke
  browser via SSE. Test regresi:
  `test_stream_one_retries_transient_error_before_first_chunk`,
  `test_stream_one_no_retry_after_first_chunk_sent` (`tests/test_fallback.py`).

### 🟡 Should-fix
- [x] `core/mcp_registry.py:56` — `mcp_servers.env` plaintext di SQLite.
  **Fully DIPERBAIKI** (2 tahap): sesi 2026-07-27 lebih dulu mitigasi
  read-path (`list_servers()` tak pernah mengembalikan `env` mentah, hanya
  `has_env`) tanpa dependency baru. Sesi 2026-07-28: owner menyetujui
  dependency `cryptography` (CLAUDE.md §7 Pengecualian #4) → write-path
  sekarang genuinely dienkripsi-at-rest via `security/vault.py::encrypt_secret`,
  bukan plaintext lagi. Detail lengkap di §4 poin 5.
- [x] `core/llm_client.py` — `httpx.AsyncClient` baru per call. **DIPERBAIKI**:
  `get_shared_http_client()`/`close_shared_http_client()` — client httpx
  pooled satu proses, timeout tetap per-call. Dipakai di `_health_check`,
  `_ollama`, `_claude`, `_gemini`, dan 2 health-check di `web/main.py`
  (startup lifespan + `/health`). Ditutup di lifespan shutdown FastAPI.
  7 test lama yang mem-patch `httpx.AsyncClient` per-panggilan
  (`tests/test_thinking.py` x4, `tests/test_settings.py` x3) disesuaikan
  reset cache global di awal test agar tetap isolated.
- [x] `core/audit.py` — 3 dimensi router (`has_code_signal`, `query_script`,
  `language_bumped`) hilang dari INSERT. **DIPERBAIKI**: kolom ditambahkan
  ke `_ADDED_COLUMNS["routing_events"]` (`infra/database.py`, idempoten
  untuk DB lama via `_ensure_columns`) dan `log_decision()` sekarang
  menyimpan ketiganya.

### 🟢 Nice-to-have
- [x] `infra/logging.py` — pattern-matching secret-scrubber solid sebagai
  defense-in-depth tapi tidak exhaustive. **Sebagian diperbaiki (2026-07-28)**:
  gap yang lebih serius ternyata bukan "format token tak dikenal", tapi
  scrub SAMA SEKALI tidak rekursif — field bersarang seperti
  `headers={"Authorization": "Bearer ..."}` atau list of dict lolos utuh
  karena key-hint & pattern cuma dicek di top-level `event_dict`, dan
  value-nya dict/list (bukan str) sebelumnya tak disentuh. `_scrub_container()`
  baru rekursif ke dict/list/tuple bersarang. Sisi "format token tak dikenal
  di field netral" tetap sebagaimana adanya (fundamental limitation
  pattern-matching, bukan sesuatu yang bisa "diperbaiki" tanpa
  allow-list/deny-list yang jauh lebih ketat). Test regresi:
  `test_scrub_recurses_into_nested_dict`,
  `test_scrub_recurses_into_list_of_dicts`,
  `test_scrub_nested_leaves_normal_values_untouched` (`tests/test_logging.py`).
  Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: 804 passed,
  ruff check/format bersih.

### ✅ Verifikasi CI sungguhan (2026-07-27, sesi lanjutan)

Mesin dev lokal cuma punya Python 3.9 (project butuh 3.12+, sintaks `X | Y`
dievaluasi eager di level anotasi fungsi tanpa `from __future__ import
annotations` → gagal import sama sekali di 3.9) dan tak ada `uv` — jadi
verifikasi awal di §2 di atas cuma `ast.parse` + `ruff` yang di-`pip install`
lepas (bukan lewat lockfile), dan **pytest belum pernah benar-benar
dijalankan**. Docker tersedia di mesin ini, jadi dipakai `python:3.12-slim` +
`uv sync --frozen --extra dev` (persis skema `.github/workflows/*.yml`) untuk
verifikasi sungguhan:

- `uv run ruff format --check .` → **133 files already formatted**.
- `uv run ruff check .` → **All checks passed!** — mengkonfirmasi ~22 temuan
  yang sempat dicurigai "drift versi ruff" di sesi sebelumnya memang ARTEFAK
  lokal (ruff 0.16.0 yang di-`pip install` lepas berbeda perilaku default dari
  `uv.lock` yang pin `0.15.18`), bukan masalah nyata. Bukan item TODO lagi.
- `uv run pytest -q` → **awalnya 42 gagal**, dua kelas bug nyata di
  perbaikan §2 sesi ini, KEDUANYA sekarang diperbaiki:
  1. **`migrations/001_initial.sql`**: 3 kolom dimensi baru
     (`dim_has_code_signal`, `dim_query_script`, `dim_language_bumped`) cuma
     ditambahkan ke `_ADDED_COLUMNS` (infra/database.py, jalur migrasi DB
     LAMA via `_ensure_columns()`), TIDAK ke `CREATE TABLE routing_events`
     langsung — banyak test membuat DB fresh via `executescript()` mentah
     (lewat `_ensure_columns()` sama sekali), jadi `log_decision()` gagal
     `sqlite3.OperationalError: no such column`. Diperbaiki: 3 kolom
     ditambahkan langsung ke `CREATE TABLE`, mengikuti pola dual-listing yang
     sudah ada untuk `evidence_json`/`human_feedback` (fresh DB dari CREATE
     TABLE, DB lama ditambal `_ensure_columns`).
  2. **`tests/test_settings.py`**: satu `FakeClient.stream()` di
     `test_gemini_sends_tools_as_function_declarations` punya signature
     sempit (`method, url, headers=None, json=None`, tanpa `**kwargs`) —
     pecah begitu `_gemini()` mulai kirim `timeout=180` (bagian dari fix
     shared-http-client). Diperbaiki: tambah `**kwargs` ke signature-nya,
     sama seperti 3 `FakeClient.stream()` lain di test suite yang sudah
     generic.
  - **Hasil akhir: 801 passed, 0 failed.**

Semua temuan §2 kini genuinely closed & terverifikasi hijau lewat CI-equivalent
sungguhan, bukan cuma statis/`ast.parse`.

**Temuan sampingan (belum diperbaiki, perlu diverifikasi manual di GitHub):**
`uv sync --frozen --extra dev` dengan `uv` versi terbaru (di-install
`astral-sh/setup-uv@v5` tanpa `version:` di-pin, sama seperti workflow CI —
lihat `.github/workflows/ci.yml`) berhasil TAPI diam-diam me-regenerate
`uv.lock` (`revision 1→3`, tambah metadata `upload-time`, DAN menambahkan
`authlib`/`joserfc`/`cryptography` yang ternyata **tidak ada** di `uv.lock`
yang di-commit — walau `pyproject.toml` sudah mensyaratkan `authlib>=1.7`
sejak fitur OIDC. Artinya `uv.lock` yang di-commit sekarang ini sudah stale
relatif ke `pyproject.toml`. Perubahan itu SUDAH DI-REVERT (`git checkout --
uv.lock`) di sesi ini — bukan scope yang diminta, dan regenerate lockfile
adalah keputusan yang butuh sepengetahuan owner (rentan reproducibility tim).
**Yang perlu dicek:** apakah CI GitHub Actions saat ini benar-benar hijau,
atau diam-diam sudah kena masalah sama (uv versi baru meregenerate lock demi
resolve authlib, lalu exit berbeda tergantung apakah `--frozen` di versi itu
strict atau lenient terhadap staleness ini). Kalau CI ternyata hijau, berarti
aman diabaikan; kalau merah, `uv.lock` perlu di-regenerate & commit ulang.

**✅ Diverifikasi 2026-07-28**: `gh run list --branch main` — commit ini
(`13ac46f`) lulus CI GitHub Actions sungguhan, status `success` (52s). Jadi
`uv` versi CI saat ini memang lenient terhadap staleness `authlib` (sama
seperti diamati lokal), CI genuinely hijau, bukan cuma asumsi. `uv.lock`
tetap stale relatif `pyproject.toml` (belum di-regenerate — keputusan
owner, §4 poin 6 lama), tapi ini terbukti TIDAK memblokir CI sekarang.
Item ini closed untuk saat ini; regenerate lockfile tetap opsional/nice-to-have
kalau owner mau kerapian, bukan lagi item mendesak.

---

## 3. Validasi arah development vs tren 2026

Dicari ulang hari ini (bukan dikutip dari KESIMPULAN.md lama yang hilang).
Sumber di §4.

| Area tren 2026 | Apa katanya | Posisi OpenCLAWN | Verdict |
|---|---|---|---|
| **Hybrid local+cloud routing** | Routing cost-aware memangkas bill LLM 40–85% tanpa penurunan kualitas terlihat; model lokal (Llama 4/Qwen 3.6/GLM-5.1) capai 80–90% performa coding GPT-4o | Ini **inti desain** OpenCLAWN sejak awal: `core/router.py` 8-dimensi + soul-aware tiering Ollama↔Claude/Gemini, plus audit kalibrasi diri | ✅ **Selaras kuat, bahkan mendahului** — router + self-calibration (Innovation 1) adalah tepat pola yang sekarang jadi mainstream |
| **Governance & observability gap** | 72% perusahaan sudah produksi tapi 60% belum punya governance formal; observability rated terendah di AI stack; evaluasi tooling kini jadi lini anggaran sendiri | Evidence-based response, `role_report`, Prometheus `/metrics`, approval sebagai entitas traceable, `actor_is_agent`/`user_id` di audit log | ✅ **Selaras kuat** — ini persis gap yang disebut riset sebagai penyebab enterprise ragu scale-up, dan OpenCLAWN sudah menutupnya lebih dulu daripada kebanyakan kompetitor open-source |
| **Model Context Protocol (MCP)** | MCP jadi standar de-facto agent↔tool; 5,000+ server publik; Gartner: 75% vendor API gateway akan tambah fitur MCP | MCP client (stdio+HTTP/SSE) + SSRF guard + `requires_approval=True` wajib, plus integrasi OpenConnector | ✅ **Selaras** — pilihan MCP (bukan protokol custom) di CLAUDE.md §7 terbukti benar; SSRF guard menaruh OpenCLAWN di sisi hati-hati yang jarang dilakukan integrasi MCP lain |
| **A2A / interoperabilitas antar-framework agent** | A2A (Agent2Agent) + ACP muncul sebagai standar terpisah dari MCP, khusus koordinasi multi-agent lintas vendor (mis. Microsoft Agent Framework 1.0 GA April 2026 sudah native MCP + adapter A2A) | OpenCLAWN punya multi-agent conversation & role handoff (Innovation 4, `roles/contracts.py`) tapi ini **internal/proprietary**, bukan protokol terbuka A2A | ⚠️ **Gap nyata tapi belum urgent** — hanya relevan kalau OpenCLAWN perlu agen-nya dipanggil ATAU memanggil agent dari platform lain (LangGraph, Microsoft Agent Framework, dll). Kandidat kuat untuk Prioritas 7 kalau interop lintas-platform jadi kebutuhan nyata |
| **Non-human identity (NHI) / agent sebagai identity principal** | NHI kini rasio 45:1–144:1 vs identitas manusia di enterprise; PAM bergeser ke zero standing privilege + just-in-time access + rotasi credential khusus agent | RBAC + OIDC + audit `actor_is_agent` sudah menandai agent sebagai aktor eksplisit; tapi credential yang dipegang agent (API key MCP eksternal, dst.) belum di-rotasi otomatis atau JIT-scoped — masih static Vault | 🟡 **Sebagian selaras** — fondasi identitas ada, tapi belum di level maturity "zero standing privilege" yang jadi arah pasar; bukan blocker untuk skala saat ini (single-deployment), tapi jadi pertimbangan kalau target multi-tenant SaaS sungguhan |
| **Self-improving skill library / agent memory** | Riset 2026 (Voyager-style, SkillForge, MemSkill) mengarah ke hal yang sama: distilasi trajectory jadi skill reusable + retrieval lebih baik — dengan catatan kritis dari literatur bahwa ini "SOP library yang lebih gemuk", bukan pembelajaran nyata | Innovation 3 (crystallizer, confidence-gated) + Innovation 2 (skill decay eksponensial) persis pola ini, dan **sudah punya gating konfiden + evaluator independen** yang justru mengantisipasi kritik "skill palsu" di literatur | ✅ **Selaras, desain lebih hati-hati dari rata-rata** — confidence-gating + evaluator-independen adalah pembeda yang belum umum di implementasi lain |

**Kesimpulan umum:** arah pengembangan OpenCLAWN **sudah sesuai, dan pada
beberapa sumbu (routing cost-aware, governance/audit trail, confidence-gated
skill learning) sudah lebih maju** dari rata-rata tooling open-source
sejenis per pertengahan 2026. Satu gap arsitektural yang layak masuk radar
adalah **interoperabilitas A2A** — bukan karena tren memaksa, tapi karena
ekosistem agent mulai berasumsi agent bisa dipanggil lintas-framework, dan
OpenCLAWN saat ini murni tertutup di dalam prosesnya sendiri.

---

## 4. Prioritas 7 (usulan, status per 2026-07-27)

1. ~~Perbaiki `EVALUATOR_FOR`~~ — **selesai**, lihat §2.
2. ~~Perbaiki retry `_stream_one`~~ — **selesai**, lihat §2.
3. ~~Shared/pooled `httpx.AsyncClient`~~ — **selesai**, lihat §2.
4. ~~Sinkronkan kolom audit dengan dimensi router~~ — **selesai**, lihat §2.
5. ~~Encryption-at-rest sungguhan untuk `mcp_servers.env`~~ — **selesai
   (2026-07-28), disetujui owner eksplisit**. `security/vault.py::encrypt_secret`/
   `decrypt_secret` (Fernet, dependency `cryptography` baru — CLAUDE.md §7
   Pengecualian sadar #4). `add_server()` mengenkripsi `env` non-kosong
   sebelum INSERT; tanpa `OPENCLAWN_ENCRYPTION_KEY` ter-set dan `env` diisi →
   `{"error": ...}` (fail loud, bukan diam-diam plaintext); tanpa `env` sama
   sekali tetap jalan tanpa key. `_config_from_row()`/`_decrypt_env_json()`
   mendekripsi untuk dipakai konek ke server sungguhan, dengan fallback ke
   parse plaintext untuk baris LAMA pra-enkripsi (backward-compat, tak
   perlu migrasi data manual). `list_servers()` tetap seperti sebelumnya —
   tak pernah mengembalikan `env` mentah maupun ciphertext, hanya `has_env`.
   Test baru: `test_encrypt_decrypt_roundtrip`, `test_encrypt_without_key_raises`,
   `test_decrypt_wrong_key_raises` (`tests/test_security.py`);
   `test_env_encrypted_at_rest`, `test_add_server_without_key_fails_when_env_given`,
   `test_add_server_without_env_needs_no_key`, `test_legacy_plaintext_env_still_readable`
   (`tests/test_mcp.py`). Docs diupdate (`docs/security.md`, `docs/core.md`,
   `.env.example`, `CLAUDE.md §7`). Diverifikasi via Docker `python:3.12-slim`
   + `uv sync --frozen` (lockfile kali ini SENGAJA diregenerate & disimpan,
   bukan direvert — penambahan dependency nyata, bukan drift; sekaligus
   menutup staleness `authlib` lama sebagai bonus): **811 passed**, ruff
   check/format bersih.
6. ~~Konfirmasi CI hijau di environment 3.12 asli~~ — **selesai**, lihat
   §"Verifikasi CI sungguhan" di §2 (801 passed via Docker `python:3.12-slim`
   + `uv sync --frozen`).
7. **[eksplorasi tren, tidak dikerjakan — dikonfirmasi owner]** Interop
   A2A/ACP — tidak ada kebutuhan pilot konkret saat ini. Biarkan sebagai
   catatan tren, jangan dibangun spekulatif.
8. **[eksplorasi tren, tidak dikerjakan — dikonfirmasi owner]** Credential
   rotation/JIT scoping untuk API key MCP eksternal — tidak ada kebutuhan
   nyata saat ini (bukan target SaaS multi-tenant). Biarkan sebagai catatan
   tren.

---

## 5. Audit susulan ops/tests/docs (2026-07-29)

Audit paralel dari 2026-07-27 (§2) sempat menjalankan 4 sub-audit sekaligus;
satu (ops/tests/docs) terputus sebelum selesai (interrupted oleh permintaan
user berikutnya) dan hasilnya tak pernah tertangkap. Dijalankan ulang penuh
hari ini sebagai kelanjutan, bukan pengulangan — semua temuan di bawah
DIVERIFIKASI manual sebelum ditindaklanjuti (satu temuan awal ternyata false
positive, lihat poin 3).

**Diperbaiki:**
1. **`.env.example` tidak lengkap** — 7 env var yang genuinely dipakai kode
   produksi (`GEMINI_BASE`, `OPENCLAWN_WORKSPACE`, `OPENCLAWN_SESSION_SECRET`,
   `OPENCLAWN_OIDC_ISSUER/CLIENT_ID/CLIENT_SECRET/REDIRECT_BASE`) sama sekali
   tak terdokumentasi — operator deploy baru tak akan tahu knob ini ada tanpa
   baca source. Ditambahkan, dicross-check lengkap terhadap SEMUA
   `os.environ.get(...)` di codebase (bukan cuma daftar dari audit awal).
2. **Tak ada `.dockerignore`** — `Dockerfile.role` pakai `COPY . .` mentah;
   `.env` (credential) dan `data/` (SQLite DB) bisa ikut ter-bake ke image
   layer kalau ada saat build. Dibuat, DIVERIFIKASI via `docker build` +
   `docker run` sungguhan: `.env`/`docs/`/`README.md` tak lagi ada di image,
   file yang dibutuhkan runtime (`core/`, `migrations/`) tetap ada, `import
   web.main` tetap sukses.
3. **`docker-compose.yml` tanpa resource limit** — satu container
   nakal/bocor memori bisa habiskan seluruh host. Ditambahkan
   `deploy.resources.limits` (2 CPU / 2GB, dihormati `docker compose` V2
   tanpa perlu Swarm — diverifikasi via `docker compose config`).
4. **`docs/core.md` stale** — `_stream_one` masih didokumentasikan
   "dengan `@retry`" (deskripsi lama, sebelum fix minggu ini) dan
   `get_shared_http_client`/`close_shared_http_client` (fix minggu ini juga)
   sama sekali tak disebut. Diupdate.

**`Dockerfile.role` jalan sebagai root** — ~~sempat ditandai "tak bisa
diverifikasi dari sandbox ini"~~ → **DIPERBAIKI 2026-08-02**, lihat §7 untuk
detail lengkap (pola entrypoint chown+`setpriv`, diverifikasi Linux VM
sungguhan via Docker Desktop, bukan diasumsikan lagi).

**False positive yang dikoreksi sendiri:** audit awal mengklaim `tenant_id`
hilang dari `CREATE TABLE routing_events`/`approval_log` di
`migrations/001_initial.sql` (analog bug `dim_has_code_signal` yang
diperbaiki minggu ini). Sebelum menerapkan fix, dicek `migrations/002_multi_tenant.sql`
(file yang sama sekali belum dibaca audit awal) — ternyata ini KEPUTUSAN
ARSITEKTUR YANG DIDOKUMENTASIKAN EKSPLISIT: kolom ini SENGAJA hanya lewat
`_ADDED_COLUMNS` untuk kedua tabel ini, bukan statis di `CREATE TABLE`. Beda
dari `dim_has_code_signal` (yang benar-benar bug): tak ada satu pun kode yang
mereferensikan `tenant_id` secara eksplisit di INSERT/SELECT untuk
`routing_events`/`approval_log`, jadi kolom yang hilang di DB fresh-test
tak pernah memicu `sqlite3.OperationalError` — genuinely harmless, bukan
cuma "belum ketahuan". Fix sempat diterapkan lalu di-revert setelah
verifikasi ini (lihat `git diff migrations/001_initial.sql` — kosong,
tak ada perubahan bersih ke file ini sesi ini).

Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **811
passed**, ruff check/format bersih, `uv.lock` tak tersentuh (tak ada
dependency baru di putaran ini).

---

## 6. Audit lapisan web/ — keamanan (2026-07-29)

Area yang belum pernah di-audit khusus minggu ini (security/sandbox,
reliability/LLM, 4 inovasi inti, dan ops/docs sudah — lihat §2 dan §5).
Semua temuan di bawah DIVERIFIKASI langsung baca kode (bukan cuma laporan
sub-agent) sebelum ditindaklanjuti.

**Diperbaiki (bug jelas, tak ambigu):**
1. **XSS via preview parameter tool** (`web/static/chat.js::statusLabel`) —
   `detail` (bisa berisi path/command/code mentah dari
   `agent_loop.py::_format_tool_params`, atau nama tool dari server MCP
   eksternal yang tak terpercaya) disuntik ke `innerHTML` TANPA escape untuk
   kasus `tool`/`tool_trusted`/`approval`/`routing`/`fallback`/`loop_stopped`
   (hanya `question` yang sudah aman). Diperbaiki: escape universal via
   `escapeHtml()` sebelum dipakai di semua cabang.
2. **5 endpoint system-config tanpa RBAC gate**: `/calibration/apply`,
   `/calibration/revert`, `/skills/set-visibility`, `/autopilots` (create),
   `/autopilots/toggle` — role apa pun yang login (termasuk `viewer`) bisa
   menggeser offset router, expose skill privat lintas-role, atau
   membuat/toggle autopilot, padahal endpoint sejenis (`/router`,
   `/autopilots/delete`) sudah admin-gated. Ditambahkan `_require_role(request,
   "admin")` konsisten pola yang sudah ada. 5 test regresi baru
   (`test_member_forbidden_from_*`, `tests/test_rbac_web.py`) — no-op saat
   auth nonaktif (default), jadi tak mengubah perilaku deployment tanpa auth.
3. **CSRF compare bukan timing-safe** (`web/main.py`) — `form_csrf !=
   cookie_csrf` diganti `hmac.compare_digest(...)`, konsisten
   `verify_login_token` yang sudah timing-safe.

Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **816
passed** (811 + 5 baru), ruff check/format bersih, `uv.lock` tak tersentuh.

**Ditemukan, BUKTI KUAT, butuh keputusan produk (bukan cuma bug fix) —
owner dikonfirmasi via percakapan, 2 dari 3 SUDAH DIPERBAIKI:**

Tiga hal berikut berakar dari SATU gap arsitektur yang sama: `session_id`
(chat) dan `approval_id` sama sekali tak terikat ke user yang login —
hanya ke `tenant_id` (default sama untuk semua user single-tenant). RBAC
(admin/member/viewer) sejauh ini HANYA menggerbangi endpoint config sistem;
tak pernah menambahkan isolasi data PER-USER untuk resource yang di-scope
per-session.

- [x] **Approval hijack lintas-user** — **DIPERBAIKI (2026-07-29/30, owner
  eksplisit konfirmasi ini harus privat per-user, bukan shared team resource)**.
  `PendingApproval.owner_user_id` (`security/approval.py`) + kolom
  `approval_log.owner_user_id` baru (dual-listed CREATE TABLE + `_ADDED_COLUMNS`,
  menghindari kelas bug yang sama dengan `dim_has_code_signal` minggu lalu).
  `ApprovalGate.request()` menerima `owner_user_id`; `pending_list()` filter
  berdasarkan owner (None = admin/auth nonaktif = tanpa filter; approval TANPA
  owner tercatat tetap terlihat semua — graceful, bukan menghilang tiba-tiba).
  `find_pending()` baru untuk cek kepemilikan di endpoint SEBELUM `resolve()`.
  `web/main.py`: `_session_owner_filter()`/`_can_access_owned_resource()`
  helper dipakai `GET /approvals` (filter list) & `POST /approve` (403 bila
  bukan pemilik & bukan admin). `core/agent_loop.py` meneruskan
  `self.cfg.user_id` (field yang sudah ada tapi tak pernah diisi dari
  `web/main.py` — sekarang diisi dari `request.state.user`) sebagai
  `owner_user_id` ke `approval.request()`.
- [x] **Chat history lintas-user (IDOR)** — **DIPERBAIKI**, pola sama.
  `chat_sessions.owner_user_id` baru (dual-listed). `ChatSessionStore.ensure_created()`
  menerima owner; `list_active(owner_user_id=...)` filter (sesi tanpa owner
  tercatat tetap terlihat semua, graceful); `get_owner()` baru untuk cek
  kepemilikan. `GET /chat-sessions` (filter list), `GET
  /chat-sessions/{id}/turns` & `DELETE /chat-sessions/{id}` (403 via
  `_can_access_owned_resource`) semua digerbangi. Admin (atau auth nonaktif)
  tetap lihat/akses semua — oversight tak hilang.
  15 test regresi baru: `tests/test_security.py` (5, ApprovalGate-level),
  `tests/test_chat_sessions.py` (5, ChatSessionStore-level),
  `tests/test_rbac_web.py` (8, end-to-end 2-user: member ditolak akses data
  user lain, member tetap akses data sendiri, admin tetap lihat/putuskan
  semua). Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`:
  **833 passed**, ruff check/format bersih, `uv.lock` tak tersentuh.
- [x] **`GET /workspace/download`** — ~~tak ada cek kepemilikan sesi~~ →
  **DIPERBAIKI 2026-07-30** (di luar cakupan pertanyaan awal yang dikonfirmasi
  owner, dikerjakan belakangan setelah desain endpoint diselesaikan — lihat
  §7 untuk detail lengkap: `session_id` opsional + fallback backward-compat).

**Sudah solid (diverifikasi, tak perlu tindakan):** path traversal guard
(`resolve_in_workspace`) dipakai konsisten di `/workspace/download`;
404/500 handler tak bocorkan stack trace/path; SSE frame selalu JSON-encoded
(tak ada frame-injection); rate limiting jalan pre-work di middleware; tak
ada CORS middleware (DELETE/PUT tak cross-site-forgeable); `/login`
open-redirect di `next=` sudah di-guard.

- [x] **Cookie `secure=True` — DIPERBAIKI (2026-08-02).** `Caddyfile.example`
  eksplisit menyatakan Caddy sebagai SATU-SATUNYA topologi produksi yang
  direkomendasikan (`reverse_proxy` Caddy secara default set
  `X-Forwarded-Proto`), jadi memercayai header ini untuk flag Secure cookie
  aman dalam konteks yang didokumentasikan proyek ini — bukan asumsi baru.
  `web/main.py::_is_secure_request()` cek `request.url.scheme == "https"
  OR X-Forwarded-Proto: https`; dipakai di kelima `set_cookie` (middleware
  refresh, `_issue_session_cookies` — sesi+CSRF, state+nonce OIDC).
  Fail-safe SIMETRIS: salah baca header ini cuma pengaruhi flag cookie
  (browser yang menegakkan, bukan server validasi apa pun berdasarnya) —
  salah positif = logout paksa, salah negatif = sama seperti sebelum
  perbaikan (bukan regresi). Test regresi: 5 baru
  (`test_is_secure_request_*`, `test_login_sets_secure_cookie_when_forwarded_proto_https`,
  `test_login_no_secure_cookie_over_plain_http`) di `tests/test_auth_web.py`.
- [x] **`Dockerfile.role` non-root — DIPERBAIKI (2026-08-02), diverifikasi
  Linux sungguhan (bukan diasumsikan).** Pola entrypoint chown-lalu-drop-
  privilege (`docker-entrypoint.sh`, baru) — BUKAN `USER appuser` statis
  (yang sempat diuji coba lalu ditolak minggu lalu, lihat §5): bind-mount
  host (`./data`) yang auto-dibuat Docker biasanya root:root, jadi chown
  SETIAP start dulu baru drop privilege via `setpriv --reuid=appuser
  --regid=appuser --init-groups` (util-linux, SUDAH ada di base image
  `python:3.12-slim` — tanpa paket tambahan). `setpriv` dipilih atas `su`/
  `sudo`: execve() langsung menggantikan proses (signal SIGTERM saat
  `docker stop` sampai tepat ke uvicorn), bukan fork+relay yang tak selalu
  reliable. **Diverifikasi langsung** (bukan diasumsikan dari macOS sandbox
  seperti percobaan minggu lalu) via Linux VM Docker Desktop sungguhan:
  simulasi bind-mount root:root (chown dari container terpisah) → tulis
  berhasil sebagai uid 1000; `/proc/1/status` konfirmasi `uvicorn` PID 1
  jalan sebagai uid 1000; `docker stop -t 5` selesai 0.36 detik (bukan
  timeout 5 detik penuh, membuktikan SIGTERM sampai & graceful shutdown
  genuinely jalan, bukan cuma asumsi) — dites dulu dengan `sleep` (gagal,
  0.36s→5s) sebelum sadar `sleep` tanpa signal handler eksplisit adalah
  kasus uji yang salah (kuirk kernel: signal tanpa handler eksplisit
  diabaikan untuk PID 1 di namespace — bukan bug `setpriv`); `uvicorn`
  punya handler SIGTERM eksplisit, jadi kasus uji yang benar. Full round-trip
  lewat `docker compose build`+`up` juga diverifikasi: healthcheck
  `(healthy)`, `/health` 200.

Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **849
passed**, ruff check/format bersih, `uv.lock` tak tersentuh.

---

## 7. Audit memory/, roles/, tools/ (2026-07-30)

Tiga area yang belum di-audit langsung minggu ini (security/sandbox, LLM
reliability, 4 inovasi inti, ops/docs, web/ sudah — lihat §2, §5, §6).

**Diperbaiki:**
1. **`tools/data.py::MemorySearchTool` — cross-role data leak, BLOCKER.**
   `requires_approval=False` DAN query `SELECT * FROM {table} WHERE {col}
   LIKE ?` sama sekali tak difilter `role` — role apa pun (pm/qa/dev/data)
   bisa baca `memory_l1`/`memory_l2`/`skills` milik role LAIN tanpa approval,
   termasuk skill `visibility='private'` (melanggar isolasi yang sama dijaga
   `SkillDecayManager.get_active_skills`, TODO.md § Prioritas 6). Diperbaiki:
   `core/agent_loop.py` menambahkan `memory_search` ke allowlist tool yang
   diberi konteks `_role` sistem (pola sudah ada untuk
   `todo_write`/`report_blocker`/`set_workdir`, BUKAN dari argumen LLM).
   `MemorySearchTool` sekarang fail-closed tanpa `_role`, filter ketat
   `role=?` untuk memory_l1/l2, dan `(role=? OR visibility IN
   ('shared','inherited'))` untuk skills — sama pola persis
   `get_active_skills`. 5 test regresi baru (`tests/test_tools_batch2.py`).
2. **`tools/document.py::_write_xlsx` — formula injection, should-fix.**
   Cell string dari konten LLM/web (bisa tak dipercaya) ditulis mentah;
   Excel men-sniff cell yang diawali `=`/`+`/`-`/`@` sebagai formula saat
   file dibuka, terlepas dari tipe cell di XML (CSV/XLSX formula injection,
   OWASP) — bukan cuma risiko CSV. Diperbaiki: `_escape_formula_cell()`
   prefix `'` untuk string yang diawali karakter pemicu; nilai non-string
   (angka, bool) tak disentuh. Test regresi:
   `test_doc_write_xlsx_escapes_formula_injection` (`tests/test_tools.py`).

Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **838
passed**, ruff check/format bersih, `uv.lock` tak tersentuh.

**Ditemukan, keputusan owner: DIBIARKAN (bukan bug yang menggantung):**
- **`tools/data.py::DbQueryTool` — tak dibatasi tabel.** Hanya blokir
  *keyword* tulis/DDL, bukan *tabel* — `SELECT * FROM users` atau
  `mcp_servers` (env terenkripsi, tapi tetap) tetap diizinkan; deskripsi
  schema di tool ("Tabel: memory_l1, memory_l2, skills, routing_events,
  role_handoffs, approval_log") aspirasional, bukan ditegakkan. **Keputusan
  owner (2026-08-02, ditanya eksplisit):** biarkan — `requires_approval=True`
  (manusia me-review SQL sebelum jalan) dianggap cukup sebagai primary
  defense. Alternatif (dependency SQL parser baru, atau redesain view-based
  access control) SENGAJA tidak diambil — bukan lupa/belum sempat.
- **`memory/layers.py::MemoryManager` tak filter `tenant_id`** — BUKAN bug
  baru, konsisten dengan scope yang SUDAH didokumentasikan eksplisit
  (`migrations/002_multi_tenant.sql`: hanya `ChatSessionStore`/`SkillDecayManager`
  wired penuh per-tenant sebagai bukti konsep; tabel lain dapat kolomnya
  tapi query BELUM difilter, "follow-up terpisah"). Dicatat di sini supaya
  tak disalahartikan sebagai gap baru oleh audit berikutnya — sama kelas
  dengan false-positif `tenant_id` di §5 yang sempat salah diperbaiki lalu
  di-revert.
- [x] **Resource exhaustion — DIPERBAIKI (2026-07-30), tanpa dependency
  baru.** `tools/web.py` (`web_fetch`/`http_request`) dan `tools/file_ops.py`
  (`file_read`/`read_many`) sebelumnya membaca SELURUH body/file ke memori
  SEBELUM dipotong ke batas. Diperbaiki: `web.py::_stream_capped()` pakai
  `client.stream()` + `aiter_text()`, berhenti membaca begitu batas
  tercapai (bukan menunggu body selesai); `file_ops.py` pakai
  `f.read(MAX_READ+1)`/`f.read(PER_FILE_BUDGET+1)` (bounded read, aiofiles
  tak perlu memuat seluruh file untuk read dengan size argument). Test
  regresi baru: `test_web_fetch_truncates_without_buffering_everything`,
  `test_file_read_truncates_large_file` (diperkuat), `test_read_many_truncates_per_file_budget`
  (`tests/test_tools.py`) — 2 test lama (`test_web_fetch_success`,
  `test_web_fetch_http_error`) disesuaikan mock-nya dari `client.get()` ke
  `client.stream()`.
- [x] **`tools/search.py::GrepTool` — ReDoS — DIPERBAIKI (2026-08-02),
  owner setujui dependency baru.** `re.compile(pattern)` dari argumen LLM
  dijalankan tanpa timeout ke semua file workspace, `requires_approval=False`
  — pattern catastrophic-backtracking bisa macetkan proses tanpa batas.
  Dependency `regex` ditambahkan (CLAUDE.md §7 Pengecualian sadar #5) —
  drop-in replacement `re` dengan parameter `timeout=` native. **Diverifikasi
  langsung sebelum memilih** (bukan diasumsikan dari dokumentasi library):
  `re.search(r'(a|a)+$', 'a'*35+'c')` genuinely hang >30s (dibunuh manual);
  `regex` module SENDIRI (independen dari timeout) masih rentan pattern yang
  sama — jadi timeout tetap wajib, ganti modul saja tak cukup; dengan
  `timeout=1.0`, pattern yang sama diinterupsi `TimeoutError` TEPAT di
  1.0001 detik. `GrepTool` sekarang set timeout per-baris, dan pada
  `TimeoutError` pertama menghentikan SELURUH pencarian (bukan cuma lewati
  baris itu — pattern yang sama akan lambat lagi di baris lain). Test
  regresi: `test_grep_redos_pattern_times_out_instead_of_hanging`
  (`tests/test_tools_workspace.py`).
- [x] **`GET /workspace/download` — DIPERBAIKI (2026-07-30), kedua temuan
  sekaligus.** Endpoint sekarang terima `session_id` opsional (`chat.js`
  selalu mengirimnya via `form.session_id.value`, sama field yang sudah
  dikirim ke `/chat/stream`/`/converse/stream`, jadi tak butuh perubahan UI).
  Bila diisi: (1) resolve ke workspace SESI via `SessionWorkspaceStore`
  (sumber sama yang dipakai `AgentLoop.run()` menentukan workdir aktif) alih-
  alih selalu `CONFIG.workspace_root` global — memperbaiki salah-folder untuk
  sesi berworkdir kustom; (2) kepemilikan dicek via `_can_access_owned_resource`
  (pola sama chat-sessions/approvals) — 403 bila bukan pemilik/admin. TANPA
  `session_id` (caller/link lama) → fallback perilaku historis (workspace_root
  global, tanpa cek kepemilikan), backward-compat penuh. Test regresi:
  `test_download_without_session_id_falls_back_to_global_root`,
  `test_download_with_session_id_resolves_session_workspace`
  (`tests/test_file_download.py`);
  `test_member_forbidden_from_downloading_other_users_session_file`,
  `test_member_can_download_own_session_file` (`tests/test_rbac_web.py`).

Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **844
passed**, ruff check/format bersih, `uv.lock` tak tersentuh.

**Status akhir §7 (2026-08-02):** dengan `GrepTool` selesai dan `DbQueryTool`
diputuskan dibiarkan, TIDAK ADA lagi item terbuka di seksi ini — semua
temuan memory/roles/tools sudah closed (diperbaiki ATAU keputusan owner
eksplisit untuk dibiarkan). Diverifikasi via Docker `python:3.12-slim` +
`uv sync --frozen`: **850 passed**, ruff check/format bersih, `uv.lock`
diregenerate & DISIMPAN (bukan direvert) — penambahan dependency `regex`
nyata & disetujui, bukan drift.

---

## 8. Prioritas 8 (usulan, belum disetujui owner) — dari riset kompetitor eve.dev, 2026-08-03

Dua ide dari membaca positioning eve.dev ("Next.js untuk agent", DX-first) yang
punya dasar teknis konkret di codebase — **bukan** sekadar meniru fitur
kompetitor, keduanya menutup gap nyata. Status: **catatan backlog, belum
ada keputusan owner untuk mengerjakan** — jangan dikerjakan tanpa
konfirmasi eksplisit (pola sama item 7-8 di §4).

1. **Durable execution — checkpoint & resume approval-pending state lintas
   restart server — ✅ SELESAI (skop: orphan cleanup + late-execute,
   2026-08-27).**

   **Keputusan desain (dipilih owner via pertanyaan eksplisit, BUKAN resume
   percakapan penuh):** dari 3 opsi yang diajukan (resume penuh / orphan
   cleanup saja / orphan cleanup + late-execute), owner memilih opsi
   tengah-atas — approval yang tersangkut dibuat terlihat lagi DAN tool-nya
   benar-benar dijalankan mandiri saat user approve, TANPA menyerialisasi
   tool loop `AgentLoop` (yang butuh perubahan arsitektur besar untuk
   kebutuhan yang belum terbukti nyata). Turn percakapan ASLI yang meminta
   approval itu tetap hilang lintas restart — user perlu tanya ulang di chat
   bila ingin agent melanjutkan dari hasil eksekusi.

   **Gap nyata yang dikonfirmasi lewat kode SEBELUM implementasi** (bukan
   asumsi): `GET /approvals` (`web/main.py`) hanya membaca
   `ApprovalGate._pending` in-memory — begitu server restart, baris
   `approval_log` yang masih `decision='pending'` jadi tak terlihat &
   tak bisa diputuskan lagi SELAMANYA, walau barisnya tetap ada di DB.

   **Hasil:**
   - `ApprovalGate.pending_list_with_orphans()` (`security/approval.py`) —
     `pending_list()` lama DIGABUNG baris `approval_log` `pending` yang tak
     punya Future in-memory lagi ("yatim"), ditandai `orphan: true`. Dipakai
     `GET /approvals`.
   - `ApprovalGate.finalize_orphan(approval_id, decision)` — selesaikan
     approval yatim (menolak bila ternyata masih live, mencegah dua sumber
     kebenaran). `ApprovalGate._record_decision` sekaligus diperbaiki: hanya
     menulis entry `audit_chain` bila `UPDATE` benar-benar mengenai baris
     (`cursor.rowcount > 0`) — sebelumnya bisa menulis entry `approval.decided`
     PALSU untuk approval yang sudah diputuskan/tak ada.
   - `core/late_execute.py::execute_orphan_approval()` — dipanggil
     `POST /approve` saat approval_id yang di-approve/reject ternyata yatim.
     Fail-closed di setiap langkah (§1): cari `role` via `chat_sessions`
     (sesi tak ditemukan → tolak), muat `soul.toml` SEGAR (bukan cache
     instance `AgentLoop` yang sudah tak ada) → cek allow-list role
     (`_soul_allows_tool`, diekstrak dari `AgentLoop._tool_allowed` jadi
     fungsi module-level di `core/agent_loop.py` — satu sumber kebenaran,
     bukan diduplikasi) → validasi schema → **evaluasi ULANG `PolicyEngine`
     deny** (policy admin bisa berubah SELAMA approval tersangkut, kadang
     berbulan-bulan — tak boleh dipercaya dari keputusan lama) → pulihkan
     folder kerja sesi dari `SessionWorkspaceStore` (§ working directory
     adaptif) → `tool.execute()` sungguhan dengan timeout & `ToolAudit`.
     `decision` ditulis `"approved:late"` (bukan `"approved"` biasa) agar
     audit trail membedakan dari approve lewat sesi live.
   - `POST /approve` (`web/main.py`): bila `find_pending()` sudah `None`,
     jatuh ke jalur baru (cek `approval_log` langsung, termasuk kepemilikan)
     alih-alih langsung gagal. Response menyertakan `executed`/`result` untuk
     jalur yatim.

   **TIDAK dikerjakan (sengaja, di luar skop yang dipilih):** resume tool
   loop/percakapan penuh via `agent_events` (Event-Driven Runtime, Prioritas
   4) — fondasinya memang ada, tapi butuh `agent_loop.py` jadi state machine
   eksplisit yang bisa di-rehydrate, perubahan arsitektur besar untuk
   kebutuhan yang belum ada bukti nyata dibutuhkan.

   Diverifikasi via `uv run --python 3.12` (mesin dev cuma Python 3.9):
   **968 passed** (+14 baru: `tests/test_durable_approval.py`,
   `tests/test_durable_approval_web.py`), ruff check/format bersih, `uv.lock`
   tak tersentuh (tanpa dependency baru). Test mencakup: orphan visible di
   `pending_list_with_orphans` tapi tidak di `pending_list` lama, tak
   dobel-hitung untuk approval live, `finalize_orphan` menolak race &
   approval yang masih live, eksekusi mandiri BENAR-BENAR menulis file ke
   workspace sesi yang dipulihkan, fail-closed untuk sesi tak dikenal & tool
   tak diizinkan role, evaluasi ulang policy deny mencegah eksekusi, dan
   endpoint `POST /approve`/`GET /approvals` end-to-end via `TestClient`
   sungguhan (bukan hanya unit `ApprovalGate`).
2. **Eval harness formal — ✅ SELESAI (2026-08-03).** `core/eval_harness.py`
   (murni logika: `EvalCase`, `load_eval_cases`, `evaluate_rubric` — dites
   pytest tanpa LLM, konsisten CLAUDE.md §5) + `scripts/run_evals.py`
   (jembatan ke `AgentLoop` SUNGGUHAN, di luar suite pytest, pola sama
   `seed_routing.py`) + `evals/dev/basic.yaml` (2 kasus contoh). Skor via
   rubrik deterministik (`contains`/`not_contains`/`tool_called`/
   `tool_not_called`/`min_length`) — BUKAN LLM-judge, sesuai arahan; kalau
   LLM-judge ditambah nanti WAJIB tunduk evaluator≥generator (I3).
   `AgentConfig(autopilot=True)` dipakai supaya tool butuh-approval diantri
   sebagai proposal (bukan menunggu manusia yang tak ada), tapi `tool_calls`
   tetap mencatat NIAT model — rubrik mengukur pilihan, bukan eksekusi.
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **954
> passed** (+22 dari 932), ruff bersih, tanpa dependency baru (`pyyaml`
> sudah ada). **Dijalankan SUNGGUHAN terhadap Ollama lokal** (`qwen2.5:3b`,
> via `uv run --python 3.12` karena mesin dev cuma Python 3.9) — bukan cuma
> diasumsikan bekerja dari unit test. Mekanisme terbukti end-to-end: setup
> workspace temporer → agent nyata jalan → tool loop → jawaban final → skor
> rubrik → laporan PASS/FAIL dengan exit code yang benar.
>
> **Dua bug NYATA di `scripts/run_evals.py` ditemukan & diperbaiki lewat run
> sungguhan** (bukan lewat review kode semata — persis metodologi "verifikasi
> empiris" yang dipakai sepanjang minggu ini):
> (a) `AgentLoop.run()` menjadwalkan `_post_turn` sebagai background task
> fire-and-forget yang MASIH JALAN setelah generator `run()` habis — DB
> berumur-pendek skrip (beda dari server produksi) ditutup terlalu cepat,
> menyebabkan `"Cannot operate on a closed database"`. Diperbaiki: tangkap
> task baru yang muncul selama `run()`, tunggu sebelum `db.close()`.
> (b) `AgentLoop.__init__` diam-diam jatuh ke `CONFIG` global (bukan
> `AppConfig` custom milik skrip) karena parameter `config=` lupa
> diteruskan — kelas bug "diam-diam salah", bukan "jelas gagal" (skrip tetap
> jalan, tapi `approval_timeout_sec`/dst yang dipakai BUKAN yang dimaksud).
>
> **Anomali `_post_turn` "no such table: memory_l1" — lihat §8.4 di bawah
> untuk root cause & perbaikan lengkap** (diinvestigasi terpisah atas
> permintaan owner setelah item ini selesai — bukan bug di `core/agent_loop.py`,
> ternyata timeout terlalu pendek di `scripts/run_evals.py` sendiri). Tetap
> bukti nyata bahwa eval harness berhasil menyingkap bug yang TAK TERLIHAT
> dari test dengan LLM di-mock, persis tujuan fitur ini dibangun — kali ini
> bug di skrip pendukungnya sendiri, bukan di kode produksi.
3. **`code_run`/`shell_run` tidak mampu menjalankan proyek nyata yang
   "lumayan besar dan kompleks" — ✅ SEBAGIAN SELESAI (skop: opsi (a), image
   kustom per-proyek Python, 2026-08-27).**

   **Keputusan desain (dipilih owner via pertanyaan eksplisit, dari 3 opsi):**
   opsi (a) — image sandbox kustom PER-PROYEK dengan dependency di-*bake*
   saat `docker build`, network HANYA terbuka DI SITU, TIDAK PERNAH saat
   `docker run` eksekusi kode sungguhan. Dipilih atas (b) (fase install
   network-terbuka saat RUN — melemahkan invarian inti §1 untuk sementara,
   permukaan `PolicyEngine`/`ApprovalGate` baru yang rawan celah `curl|sh`)
   dan (c) (cuma perbaikan timeout — tak menyentuh gap `pip install` sama
   sekali, sub-masalah paling kecil dari tiga yang dilaporkan).

   **Hasil:**
   - `infra/sandbox_image.py` (baru) — `CURRENT_SANDBOX_IMAGE` (ContextVar) +
     `SessionSandboxImageStore`, PERSIS pola `infra/workspace.py`
     (`CURRENT_WORKSPACE_ROOT`/`SessionWorkspaceStore`, § working directory
     adaptif) — image proyek aktif per-sesi bertahan lintas turn DAN lintas
     restart server (memakai pola durability yang sama dipelajari §8.1).
   - `DockerSandbox.build_project_image()` (`tools/sandbox.py`) — image baru
     dibangun `FROM SANDBOX_IMAGE` dasar (mewarisi semua properti keamanan
     lain) di build context TERISOLASI (temp dir HANYA `requirements.txt` +
     `Dockerfile` ter-generate, bukan seluruh workspace) via `pip install`.
     **Satu-satunya** invocation `docker` di modul ini yang sengaja TANPA
     `--network none`. Cache: `docker image inspect` (hash SHA-256 12-char
     konten `requirements.txt` sebagai tag) SEBELUM build — skip rebuild
     TANPA network sama sekali bila sudah pernah dibangun. Timeout build
     300 detik dengan `proc.kill()` eksplisit (tak ada wrapper `timeout`
     command portabel di level host seperti `run_python`/`run_shell`, yang
     timeout-nya jalan DI DALAM container).
   - `DockerSandbox._base_docker_args` membaca `effective_sandbox_image()` —
     `code_run`/`shell_run` OTOMATIS memakai image proyek aktif tanpa
     perubahan lain, sesi yang tak pernah membangun tetap `SANDBOX_IMAGE`
     dasar (perilaku lama tak berubah).
   - Tool baru `build_sandbox_image` (`tools/sandbox_image.py`) —
     `requires_approval=True` **non-negotiable**, ditambahkan ke
     `_TRUST_MODE_EXEMPT` sekelas `code_run` (build-nya sendiri membuka
     network). `_validate_requirements`: tolak kosong, >20.000 byte, >200
     baris, atau baris manapun yang diawali `-` (opsi pip `-e`/`--index-url`/
     `-r`/dst) — mencegah pengalihan sumber paket ke index tak tepercaya
     atau instalasi VCS/lokal arbitrer. Ditambahkan ke allow-list role
     `dev`/`qa`/`data` (role yang sudah punya `code_run`), TIDAK `pm`/`security`.
   - **Residual risk didokumentasikan jujur (§1/§17):** `pip install` bisa
     menjalankan kode arbitrer dari `setup.py`/build backend paket pihak
     ketiga SELAMA build — risiko inheren pip apa pun sumbernya, validasi di
     atas hanya mempersempit permukaan (PyPI resmi saja), tidak menghapusnya.

   **Belum ditangani (skop MVP, dicatat eksplisit — bukan diabaikan diam-diam):**
   hanya Python/`requirements.txt` — Node/`package.json` dkk belum didukung;
   tidak ada garbage collection image `openclawn-sandbox-proj:*` (operator
   perlu `docker image prune` manual); (b) fase-install-network-terbuka dan
   (c) timeout/resource configurable TETAP backlog terbuka bila dibutuhkan
   nanti (proses >30 detik yang bukan soal dependency, runtime non-Python,
   service yang listen di port — semua ini TETAP tidak bisa jalan lewat
   jalur ini).

   Diverifikasi via `uv run --python 3.12`: **993 passed** (+25:
   `tests/test_sandbox_image.py` 24 test + 1 di `tests/test_trust_mode.py`),
   ruff check/format bersih, `uv.lock` tak tersentuh (tanpa dependency baru).
   **Smoke test SUNGGUHAN dengan Docker nyata** (bukan cuma mock) —
   `docker build -t openclawn-sandbox:latest -f Dockerfile.sandbox .` lalu
   skrip terisolasi yang membuktikan END-TO-END: (1) `termcolor` TAK bisa
   di-import di image dasar; (2) `build_project_image("termcolor==2.4.0")`
   sukses BENAR-BENAR install via PyPI nyata; (3) build kedua dengan konten
   SAMA → `cached=True`, 0.02 detik (vs 0.4 detik build pertama) — TANPA
   network; (4) dengan `CURRENT_SANDBOX_IMAGE` diaktifkan, `run_python`
   BENAR-BENAR bisa `import termcolor` & memanggilnya; (5) tanpa override,
   balik ke image dasar → `termcolor` TAK bisa di-import lagi (isolasi
   per-sesi terbukti, bukan image dasar yang diam-diam tertimpa); (6) baris
   `--index-url` di `requirements.txt` DITOLAK sebelum sampai ke `docker
   build`. Image proyek uji coba dihapus (`docker rmi`) setelah verifikasi;
   `openclawn-sandbox:latest` (base) dibiarkan ada untuk pemakaian berikutnya.
4. **[Ditemukan lewat eval harness §8.2] `_post_turn` melempar `"no such
   table: memory_l1"` — ✅ ROOT CAUSE DITEMUKAN & DIPERBAIKI (2026-08-03).**

   **BUKAN bug di `core/agent_loop.py`/`memory/layers.py`** seperti diduga
   semula — murni bug di `scripts/run_evals.py` sendiri. Dibuktikan lewat
   reproduksi terisolasi bertahap (bukan tebakan): repro sederhana (1 kasus,
   tanpa tool loop) TIDAK memicu bug; repro dengan `tool_loop_detected` yang
   PERSIS sama dengan kasus gagal JUGA tidak memicu; baru reproduksi lewat
   **dua kasus berurutan dalam satu proses** (persis alur `_main()` sungguhan)
   berhasil memicu bug secara konsisten — mengarahkan curiga ke interaksi
   ANTAR-kasus, bukan satu kasus tunggal.

   **Akar masalah sesungguhnya:** `_run_one_case` menunggu background task
   `_post_turn` selesai HANYA 10 detik sebelum `db.close()` — tapi
   `_post_turn` sendiri memanggil `_generate_session_title()` yang bisa
   memicu cascade fallback LLM hingga 4 model × retry+backoff (>10 detik
   saat Ollama lambat/model default `compaction_local_model` tak tersedia
   lokal). Saat timeout 10 detik itu terlampaui, kode LAMA menutup DB
   TANPA MENGECEK apakah task benar-benar selesai — `_post_turn` KASUS INI
   lanjut berjalan DI BACKGROUND sementara KASUS BERIKUTNYA sudah mulai,
   lalu menabrak DB yang sudah tertutup. Muncul sebagai `"no such table"`
   (bukan `"Cannot operate on a closed database"` yang lebih jelas) karena
   closure terjadi di tengah operasi yang sudah diantre di aiosqlite, bukan
   sebelum operasi dimulai.

   **Perbaikan:** timeout dinaikkan ke 60 detik (melebihi worst-case
   realistis) DAN kode sekarang MENGECEK EKSPLISIT apakah task masih
   `pending` setelah timeout — bila ya, DB SENGAJA TIDAK ditutup (dibiarkan
   bocor sampai proses keluar, jauh lebih aman daripada merusak task yang
   masih jalan) dan operator diberi peringatan jelas untuk cek kesehatan
   Ollama. Diverifikasi ulang dengan reproduksi PERSIS SAMA yang tadinya
   gagal 2× berturut-turut — sekarang bersih, tanpa error, di kedua kasus.

---

## 9. Prioritas 9 (usulan, belum disetujui owner) — riset tren & market, 2026-08-03

Empat arah dari riset web terarah (sumber di § Sumber riset tren, batch
2026-08-03). Status: **catatan backlog, belum ada keputusan owner** — jangan
dikerjakan tanpa konfirmasi eksplisit (pola sama §4 item 7-8 dan §8).

Diurutkan berdasarkan **urgensi pasar × kesiapan fondasi kode**, bukan
kemudahan implementasi.

### 9.1 Tamper-evident audit trail (hash chaining) — ✅ SELESAI (2026-08-03)

> **Dikerjakan atas arahan owner eksplisit** ("utamakan tren dan market pasar,
> buat OpenCLAWN tetap valid beberapa tahun ke depan") — dipilih dari 4 kandidat
> §9 karena menang di KEDUA kriteria itu: regulasi sudah berlaku (bukan tren
> opsional) dan regulasi bertahan bertahun-tahun (beda dari spesifikasi
> framework yang bisa berubah — bandingkan §9.3 yang justru ditunda karena
> spec-nya belum stabil).
>
> **Hasil:** `core/audit_chain.py` + tabel `audit_chain` (append-only,
> SHA-256 chained) + `GET /audit/verify` (admin-only, mengembalikan `head`
> untuk anchoring). Di-wire ke `RoutingAuditor` (decision/finalized) dan
> `ApprovalGate` (requested/decided/**auto** — trust mode dirantai justru
> karena MELEWATI klik manusia). Fungsi SQLite `SHA256` didaftarkan
> `DatabaseManager` (pola sama `POWER()`) supaya penulisan rantai ATOMIK dalam
> satu statement — mencegah rantai bercabang saat dua turn bersamaan, yang akan
> tampak sebagai "rantai rusak" padahal tak ada manipulasi.
>
> **Keputusan desain kunci:** tabel TERPISAH, bukan kolom hash di
> `routing_events`/`approval_log` — kedua tabel itu DIMUTASI setelah INSERT
> (`finalize`, `check_correction` di turn berikutnya, `set_human_feedback`),
> jadi hash in-place tak akan pernah verify. Append-only menghindari itu
> sepenuhnya, dan justru membuat urutan "diputuskan→diselesaikan→dikoreksi"
> terlihat sebagai sejarah.
>
> **Batas jaminan didokumentasikan JUJUR & dikunci test** (bukan diklaim lebih):
> hash chain membuat perubahan retroaktif TERDETEKSI, bukan MUSTAHIL.
> Penghapusan entry terakhir (truncation) dan penulisan-ulang seluruh rantai
> TIDAK tertangkap `verify()` — hanya oleh anchoring (menyalin `head` ke luar
> sistem). Dua test sengaja mengunci fakta ini
> (`test_detects_deleted_last_entry_only_via_anchor`,
> `test_rewriting_whole_chain_is_not_detected_by_verify_alone`) supaya tak ada
> yang mengubahnya jadi klaim "immutable" yang berlebihan di README/UI.
> Tanda tangan per-entry (ECDSA) di luar scope — butuh manajemen kunci yang
> belum ada.
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **863
> passed** (+13 baru), ruff check/format bersih, `uv.lock` tak tersentuh (tanpa
> dependency baru — `hashlib` stdlib). Verifikasi manual end-to-end lewat jalur
> nyata (`RoutingAuditor` + `ApprovalGate`) mengonfirmasi rantai terbentuk,
> `verify()` hijau saat utuh, dan manipulasi entry `approval.auto` terdeteksi
> tepat di entry ke-3 dengan alasan yang benar.
>
> **Follow-up (c) anchoring — ✅ SELESAI (2026-08-03).** `core/audit_anchor.py`
> (`write_anchor`/`verify_against_anchors`) + `scripts/anchor_audit_chain.py`
> (cron/systemd, pola sama `backup_db.py`, exit code 1 bila `--verify` gagal
> untuk alerting) + `GET /audit/verify` diperluas field `anchors` + `POST
> /audit/anchor` (trigger manual). Terverifikasi menangkap KEDUA serangan yang
> `verify()` sendirian TAK bisa (truncation & rewrite penuh) — dua test
> mereproduksi persis skenario itu dan konfirmasi tertangkap. Batas jaminan
> anchoring ITU SENDIRI didokumentasikan jujur (file lokal baru jadi anchor
> sungguhan setelah disalin off-host — kebijakan penyalinan tetap di luar
> scope kode, README § Self-hosting menjelaskan cara pakainya).
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **917
> passed** (+22 dari 902 — termasuk 6 dari §9.4/§9.2 sebelumnya), ruff bersih,
> tanpa dependency baru, `uv.lock` tak tersentuh. Skrip CLI dieksekusi
> sungguhan (bukan cuma unit test): seed 2 entry → anchor → verify OK → hapus
> 1 entry (simulasi truncation) → verify GAGAL dengan exit code 1.
>
> **Follow-up (b) retensi 6 bulan — ✅ SELESAI (2026-08-03), keputusan owner:
> pagar kode.** Ditanya eksplisit ("pagar kode" vs "dokumentasi saja"), owner
> pilih pagar kode. Temuan sebelum eksekusi: proyek ini TIDAK punya pruning
> sama sekali untuk 3 tabel ini — jadi retensi terpenuhi trivial hari ini;
> gapnya adalah TIDAK ADA PAGAR untuk kode pruning masa depan.
>
> `infra/retention.py` (`MIN_RETENTION_DAYS = 180`, satu sumber kebenaran
> untuk dokumentasi/test) + 3 trigger SQLite (`trg_retention_routing_events`,
> `trg_retention_approval_log`, `trg_retention_audit_chain`) di
> `migrations/001_initial.sql` — `BEFORE DELETE ... WHEN (umur < 180 hari)
> RAISE(ABORT)`. Penegakan di level DATABASE (bukan fungsi Python yang bisa
> lupa dipanggil) — tak bisa dilewati jalur kode mana pun. Diverifikasi
> LANGSUNG sebelum dipakai: `RAISE(ABORT)` tembus sebagai `sqlite3.IntegrityError`
> lewat aiosqlite; batch DELETE campuran tua+muda di-rollback SELURUHNYA
> (fail-closed — baris tua yang boleh dihapus pun ikut bertahan).
>
> **Efek samping berharga yang ditemukan saat implementasi:** untuk data
> < 180 hari, trigger ini membuat serangan truncation/rewrite-lewat-delete
> terhadap `audit_chain` (batas jaminan follow-up (c) di atas) **mustahil
> secara struktural**, bukan cuma terdeteksi anchoring.
>
> **Regresi yang ditemukan & diperbaiki SEBELUM commit:** `scripts/seed_routing.py --clear`
> akan gagal (trigger memblokir DELETE data seed yang baru diinsert) — dan 5
> test lama (`test_audit_chain.py`, `test_audit_anchor.py`) yang men-DELETE
> entry segar untuk simulasi tampering ikut gagal. Diperbaiki: seed data
> di-backdate `MIN_RETENTION_DAYS + 5` hari (bukan pengecualian di trigger —
> trigger tetap berlaku sama untuk semua baris); test lama pakai helper
> `_append_backdated` untuk tetap menguji skenario yang sama pada data tua.
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **926
> passed** (+9 baru, +0 regresi bersih setelah perbaikan), ruff bersih, tanpa
> dependency baru. Migrasi dikonfirmasi idempoten (executescript dijalankan
> 2× berturut-turut, simulasi restart server, tak error).
>
> **Batas yang sengaja belum diselesaikan:** tegangan dengan permintaan
> penghapusan GDPR (PII di `query_text`/`tool_input`) — butuh desain redaksi
> konten, kelas masalah berbeda, di luar scope perubahan ini.
>
> **Follow-up (a) chain had_correction/human_feedback — ✅ SELESAI
> (2026-08-03), DIREVISI dari keputusan awal.** Ditinjau ulang atas
> permintaan owner: keduanya BUKAN cuma sinyal kalibrasi — mereka bukti
> tindakan agent ternyata bermasalah menurut user, relevan-langsung EU AI
> Act Article 12. Menyimpannya HANYA sebagai UPDATE biasa (bukan rantai)
> berarti siapa pun dengan akses tulis DB bisa diam-diam menghapus jejak
> user pernah mengoreksi/memberi rating buruk — celah yang seharusnya
> ditutup fitur ini.
>
> Dua entry type baru (`routing.corrected`, `routing.human_feedback`).
> `check_correction()` di-refactor SELECT-id-dulu → UPDATE-by-id (supaya
> `ref_id` benar tersedia untuk chain, DAN entry rantai hanya ditulis bila
> BENAR ADA event sebelumnya — turn pertama tak menulis entry palsu).
> Kontrak return value **tak berubah** (dikunci test eksplisit) — tak
> mengganggu `SkillFeedback.resolve_previous` yang sudah bergantung
> padanya. `set_human_feedback()` hanya chain bila rating valid & event
> ditemukan.
>
> Diverifikasi manual end-to-end skenario paling relevan: user mengoreksi
> jawaban → penyerang coba hapus jejak koreksi → **ditolak ganda** oleh
> append-only design DAN trigger retensi 180 hari (follow-up (b) di atas —
> dua follow-up ini sekarang saling menguatkan).
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **932
> passed** (+6), ruff bersih, tanpa dependency baru, 0 regresi.
>
> **Dengan ini, SEMUA follow-up § Prioritas 9.1 sudah selesai** — tak ada
> lagi item terbuka di bawah entry ini.

**Konteks & justifikasi asli (dipertahankan):**

**Temuan riset yang mengubah prioritas:** EU AI Act Article 12 (kewajiban
*automatic recording of events* untuk sistem AI risiko-tinggi) menjadi
**enforceable 2 Agustus 2026 — KEMARIN**, dengan penalti hingga €15 juta atau
3% omzet global. Article 12 menuntut log yang (a) otomatis tanpa intervensi
operator, (b) mencakup seluruh lifetime sistem, (c) retensi minimal 6 bulan,
(d) cukup untuk traceability penuh input→output→decision point.

**Gap nyata di OpenCLAWN:** README menjual pilar **"Immutable Audit Evidence"**,
tapi secara teknis klaim itu **belum benar**. `routing_events`, `approval_log`,
dan `agent_events` hanyalah baris SQLite biasa — tak ada mekanisme apa pun yang
membuat modifikasi/penghapusan retroaktif terdeteksi. Siapa pun dengan akses
file DB bisa mengubah riwayat tanpa jejak. Ini bukan cuma kekurangan fitur:
ini **klaim marketing yang belum didukung implementasi**, kelas masalah yang
sama dengan temuan-temuan §2-§7 (dokumentasi/klaim ≠ kode nyata).

**Arah kandidat (minimal, konsisten CLAUDE.md §8 "paling sederhana"):**
tambah kolom `prev_hash` + `record_hash` ke tabel audit; `record_hash =
SHA-256(canonical_json(baris ini) + prev_hash)`. Verifikasi = satu query
scan yang mengecek rantai. **Bukan** blockchain (tak perlu konsensus/
distribusi), **bukan** dependency baru (`hashlib` stdlib). Titik desain yang
perlu diputuskan: (a) tabel mana yang dirantai — semua atau `routing_events` +
`approval_log` saja; (b) apakah retensi 6 bulan perlu ditegakkan kode
(pruning saat ini tak ada) atau cukup didokumentasikan sebagai tanggung jawab
operator; (c) canonicalization JSON — RFC 8785 (JCS) adalah rujukan standar,
tapi implementasi manual `json.dumps(sort_keys=True, separators=...)` mungkin
cukup untuk kasus ini (perlu diverifikasi, jangan diasumsikan).

**Konteks standardisasi (jangan dijadikan patokan buta):** ada Internet-Draft
IETF `draft-sharif-agent-audit-trail-00` yang mendefinisikan format JSON audit
agent + hash chaining SHA-256 per RFC 8785 + tanda tangan ECDSA opsional, dan
memetakan diri ke EU AI Act, SOC 2, ISO/IEC 42001, PCI DSS v4.0.1. **Caveat
penting:** ini *individual submission*, BUKAN dokumen working-group yang sudah
diadopsi IETF — draft `draft-sharif-*` lain dari penulis yang sama ada banyak
(identity framework, ATTP, AEBA, payment trust), dan draft ini kedaluwarsa
29 September 2026. Perlakukan sebagai **sinyal arah & referensi struktur
field**, bukan standar yang wajib diikuti. Yang mengikat secara hukum adalah
EU AI Act-nya, bukan draft ini.

### 9.2 Identitas agent sebagai first-class citizen (Non-Human Identity) — ✅ SELESAI (2026-08-03)

> **Hasil:** `core/agent_identity.py` — `agent_identity(role, soul)` →
> `"{role}@{hash12}"`, hash SHA-256 dari SELURUH `soul.toml` efektif
> (canonical JSON, bukan subset field pilihan tangan — field baru di
> `soul.toml` masa depan otomatis ikut tercermin tanpa perlu mengingat
> memperbarui modul ini). Dihitung sekali di `AgentLoop.__init__`, diteruskan
> ke `RoutingAuditor.log_decision()` dan `ApprovalGate.request()`/`auto_approve()`
> — kolom `agent_identity` baru di `routing_events` & `approval_log`, DAN ikut
> ke payload `audit_chain` (melengkapi §9.1 secara literal, bukan cuma niat).
> `RoutingAuditor.identity_report()` + `GET /metrics/identities` menjawab
> pertanyaan yang diajukan di bawah: agregasi `(role, agent_identity)` dengan
> rentang waktu — satu role dengan >1 identitas berarti config-nya pernah berubah.
>
> Kolom index (`idx_routing_agent_identity`) sengaja TIDAK statis di
> `migrations/001_initial.sql` — dibuat `DatabaseManager._ensure_columns()`
> SETELAH kolom ditambal ke DB lama, pola sama `idx_approval_id`/`idx_l2_role`
> (bug class yang sama sudah terulang 3× sebelumnya, ditangkap sebelum jadi
> yang ke-4).
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **902
> passed** (+21 dari 881), ruff check/format bersih, `uv.lock` tak tersentuh
> (tanpa dependency baru). Verifikasi manual dengan `soul.toml` role `dev`
> sungguhan: mencabut `code_run` dari tool allow-list menghasilkan identitas
> BERBEDA (`dev@66c2a14cb44c` → `dev@bb57d625a090`), `identity_report()`
> memisahkan keduanya dengan benar, dan payload `audit_chain` untuk
> `approval.auto` membawa identitas yang tepat.
>
> **Follow-up yang sengaja belum dikerjakan:** tak ada UI tabel HTML untuk
> `identity_report` di `/metrics` (hanya JSON, pola sama `role_report` yang
> juga JSON-only) — bisa ditambahkan kalau memang dibutuhkan, bukan
> dikerjakan spekulatif.

**Konteks & justifikasi asli (dipertahankan):**

**Validasi pasar kuat:** 91% organisasi sudah memakai AI agent tapi hanya 10%
punya strategi matang mengelola identitas agent tersebut. NHI kini melampaui
identitas manusia **144:1** di lingkungan cloud-native (naik dari 92:1 awal
2024). Pasar NHI access management tumbuh >40% CAGR sampai 2030 — salah satu
segmen keamanan enterprise tercepat; Palo Alto mengakuisisi CyberArk senilai
$25 miliar (Februari 2026) untuk menyatukan PAM + machine identity.

**Statistik yang paling relevan langsung:** **68% organisasi tidak bisa
membedakan aktivitas AI agent dari aktivitas manusia** (survei Cloud Security
Alliance). OpenCLAWN sudah menjawab sebagian ini — kolom `actor_is_agent`
(selalu `1`) di `routing_events` & `approval_log`, plus `owner_user_id`
(audit 2026-07-29) yang memisahkan "user mana yang memicu". Jadi fondasinya
ADA, tapi masih **flag biner**, belum konsep identitas.

**Arah kandidat:** identitas stabil per *(role + versi konfigurasi)* — bukan
sekadar nama role — supaya pertanyaan audit "agent dengan konfigurasi mana
yang melakukan X pada tanggal Y" bisa dijawab lintas sesi, bahkan setelah
`soul.toml` berubah. Kandidat implementasi ringan: hash konten `soul.toml`
efektif (termasuk `[policy]` & tool allow-list) disimpan per turn, sehingga
perubahan permission agent terlihat di jejak audit. Ini melengkapi 9.1 —
hash chaining membuktikan log tak diubah; identitas agent menjawab log itu
*tentang siapa*.

**Beda dari yang sudah ditolak (§4 item 8):** item itu soal *credential
rotation/JIT scoping untuk API key MCP eksternal* — ditolak karena bukan
target SaaS multi-tenant. Ini soal **identitas & atribusi untuk audit**,
kebutuhan berbeda yang justru menguat karena 9.1 (EU AI Act butuh
traceability, bukan cuma penyimpanan).

### 9.3 Exporter OpenTelemetry GenAI — TUNGGU DULU, ada alasan teknis

**Temuan riset yang MENGUBAH rekomendasi awal saya.** Dugaan awal: "OTel makin
jadi lingua franca, tinggal bikin exporter". Fakta per pertengahan Juli 2026:
**setiap atribut/span/metric `gen_ai.*` di registry resmi OpenTelemetry masih
berstatus "Development" — TIDAK SATU PUN sudah "Stable".** Agent Spans dan
konvensi MCP justru yang paling baru & paling belum settle.

Adopsi nyata memang sudah ada (VS Code Copilot, OpenAI Codex, Claude Code
[beta] mengemisi trace OTel GenAI; Datadog mendukung natif) — jadi arahnya
benar. Tapi membangun exporter penuh sekarang berarti **menanggung churn
spesifikasi** untuk standar yang penulisnya sendiri belum bekukan.

**Rekomendasi jujur:** JANGAN bangun exporter penuh sekarang. Bila mau
bergerak, batasi ke *adapter tipis* di atas `core/prometheus_metrics.py` yang
sudah ada — satu titik pemetaan nama field, sehingga saat konvensi stabil
(kemungkinan 2027) perubahan terbatas di satu file, bukan tersebar. Tinjau
ulang status "Stable" sebelum investasi lebih besar. Dicatat di sini
justru supaya **tidak** dikerjakan prematur karena terdengar modern.

### 9.4 Dashboard penghematan biaya dari hybrid routing — ✅ SELESAI (2026-08-03)

> **Temuan yang mengubah rencana saat dikerjakan:** premis "semua data mentah
> sudah tersimpan (`cost_usd`)" di bawah ini **TERNYATA SALAH**. `SmartRouter.MODELS`
> dan `RouterConfigStore.get_map()` menyetel `cost_per_1k=0.0` untuk SEMUA
> tier secara sengaja ("cost nyata tak dipetakan; jangan tebak" — untuk
> keputusan routing live) — akibatnya `routing_events.cost_usd` SELALU 0.0,
> bukan cuma kadang. Kalau dikerjakan persis sesuai rencana awal (agregasi
> `cost_usd`), hasilnya "$0 hemat dari $0" — fitur kosong yang terlihat jalan.
>
> **Solusi:** `core/cost_pricing.py` (baru) — tabel harga publik
> bertanggal-verifikasi (Ollama gratis, Gemini Flash/Pro, Claude Haiku/Sonnet
> — diverifikasi via web search, sumber dicatat di modul), dipakai untuk
> menghitung ULANG biaya dari `model_chosen`+token, BUKAN membaca `cost_usd`.
> `estimate_cost_usd()` mengembalikan `None` (bukan `0.0`) untuk model tak
> dikenal — prinsip "jangan tebak" yang sama, diterapkan ke pelaporan
> retrospektif alih-alih keputusan live.
>
> **Hasil:** `RoutingAuditor.cost_savings_report()`, `GET /metrics/cost-savings`
> (JSON), kartu ringkasan di `/metrics` (HTML) — 3 angka (estimasi hemat,
> biaya aktual, biaya counterfactual "jika semua ke `gemini-2.5-pro`") + label
> `is_estimate: true` SELALU ditampilkan + disclaimer eksplisit (tak
> memperhitungkan prompt caching/batch discount/kontrak kustom). Titik desain
> di bawah ("apakah counterfactual ditampilkan sebagai estimasi eksplisit")
> dijawab: YA, selalu, tanpa kecuali — pola sama batas-jaminan hash chain di
> §9.1: jangan klaim lebih dari yang bisa dibuktikan.
>
> Diverifikasi via Docker `python:3.12-slim` + `uv sync --frozen`: **881
> passed** (+18 dari 863), ruff check/format bersih, `uv.lock` tak tersentuh
> (tanpa dependency baru). Verifikasi manual: seed 2 routing_events (1
> `gemini-2.5-pro` mahal, 1 `gemma4:e4b` gratis) → `/metrics/cost-savings`
> mengembalikan $7.5 aktual / $21.25 counterfactual / $13.75 hemat (64.7%) —
> dihitung ulang manual, cocok persis. Render HTML `/metrics` dikonfirmasi
> menampilkan kartu + disclaimer dengan benar.
>
> **Follow-up yang sengaja belum dikerjakan:** tabel harga perlu ditinjau
> ulang berkala (harga API cloud berubah) — `PRICING_VERIFIED_ON` di modul
> jadi penanda kapan terakhir dicek, tak ada mekanisme otomatis untuk
> memperbarui atau memperingatkan staleness.

**Konteks & justifikasi asli (dipertahankan):**

**Validasi pasar:** model routing memangkas biaya LLM nyata **40-85% tanpa
penurunan kualitas terlihat**; riset ICLR 2025 mencapai penghematan 85% pada
MT-Bench di 95% kualitas GPT-4, dengan model kuat hanya dipakai untuk 14%
query. Untuk workload enterprise volume tinggi, hybrid + routing ke model
kecil menghasilkan blended cost 50-85% lebih rendah.

**Kesiapan fondasi: TERTINGGI dari keempat item ini.** Semua data mentah
SUDAH tersimpan per turn di `routing_events` — `tokens_in`, `tokens_out`,
`cost_usd`, `model_chosen`, `provider`, `complexity_label`. Yang belum ada
hanyalah **agregasi + tampilan**: "berapa yang dihemat dibanding skenario
semua-query-ke-cloud-tier-tertinggi?" Perhitungan counterfactual sederhana
(tokens aktual × tarif model termahal di `MODELS`) minus biaya aktual.

Tak butuh dependency baru, tak butuh perubahan skema, tak menyentuh jalur
keamanan mana pun — kandidat paling murah dengan cerita ROI paling konkret
(dan paling mudah didemonstrasikan ke calon pengguna). Titik desain: apakah
angka counterfactual ditampilkan sebagai estimasi eksplisit (jujur: itu
skenario hipotetis, bukan tagihan nyata yang dihindari) — penting supaya
tidak jadi klaim yang menyesatkan seperti kasus "Immutable Audit Evidence"
di 9.1.

---

## 10. Audit lapisan `security/` — internal modul (2026-08-27)

Babak audit baru (pola sama §2/§5/§6/§7 — dikerjakan atas permintaan eksplisit
owner setelah backlog §8/§9 bersih semua). Fokus: internal modul `security/`
sendiri (`auth.py`, `rate_limit.py`, `shield.py`, `question.py`,
`policy_engine.py`, `guardrails.py`, `oidc.py`, `skill_scanner.py`,
`vault.py`) — beda dari §6 yang mengaudit LAPISAN ENDPOINT `web/`. Semua
temuan di bawah DIVERIFIKASI langsung baca kode + reproduksi terisolasi
(bukan cuma laporan/asumsi) sebelum ditindaklanjuti, sama metodologi audit
sebelumnya.

**Diperbaiki (bug jelas & satu gap IDOR, tak ambigu):**

1. **`RateLimiter` — kunci rate-limit tak stabil saat idle timeout aktif,
   BLOCKER untuk deployment yang mengaktifkan `OPENCLAWN_IDLE_TIMEOUT_SEC`.**
   `web/main.py`'s middleware memakai nilai cookie sesi MENTAH sebagai key
   rate-limit — tapi `create_session_token` menyisipkan `ts` BARU tiap kali
   cookie di-refresh (§ idle timeout, `security/auth.py`), yang terjadi TIAP
   REQUEST VALID saat `idle_timeout_sec` diisi. Diverifikasi lewat reproduksi
   terisolasi (bukan diasumsikan): `RateLimiter(max_requests=3)` + 10 token
   sesi ber-`ts` berbeda untuk user yang SAMA → **10/10 request lolos**
   (rate limit sama sekali tak efektif) DAN 10 entry berbeda tersimpan
   PERMANEN di `_hits` (kebocoran memori tanpa batas — satu entry baru per
   request, key lama tak pernah dipakai lagi jadi tak pernah dibuang).
   Diperbaiki: key = `f"user:{session_user_id}"` (stabil lintas refresh
   cookie MAUPUN lintas device/re-login akun yang sama) bila auth aktif &
   user dikenal, fallback cookie/IP untuk kasus lama. Sekaligus diperbaiki
   `RateLimiter.remaining()`: membaca `self._hits[key]` (bukan `.get(key,
   [])`) pada `defaultdict` diam-diam MENAMBAH entry permanen hanya karena
   dibaca — method belum dipakai endpoint mana pun saat ini (disiapkan untuk
   header `X-RateLimit-Remaining`), jadi tak reachable di produksi, tapi
   diperbaiki sebagai pencegahan sebelum benar-benar dipakai.
2. **`POST /answer` tanpa cek kepemilikan sama sekali — IDOR, sekelas bug
   approval-hijack yang diperbaiki §6.** Satu-satunya endpoint session-scoped
   yang LOLOS dari audit kepemilikan 2026-07-29 (`/chat-sessions/*`,
   `/approve` sudah digerbangi saat itu) — `QuestionGate` sendiri tak
   menyimpan `owner_user_id` (beda dari `ApprovalGate`), jadi gap ini tak
   ketahuan lewat pola pencarian yang sama. User login mana pun (termasuk
   role terendah) bisa menjawab pertanyaan klarifikasi `ask_user` milik sesi
   USER LAIN hanya dengan menebak/mengetahui `session_id`-nya, menyuntikkan
   jawaban PALSU ke tengah turn agent orang lain. Diperbaiki: kepemilikan
   dicek via `chat_sessions.owner_user_id` (tabel yang SAMA dipakai endpoint
   sesi lain, bukan menambah state baru) + `_can_access_owned_resource`,
   pola identik `/chat-sessions/{id}/turns`.
3. **`security/skill_scanner.py` — deteksi `open(path, mode="w")` (keyword)
   sepenuhnya lolos, bypass trivial dari sinyal file-write scanner.**
   `_scan_ast` hanya mengecek `node.args[1:]` (argumen posisional) untuk mode
   `open()`, mengabaikan `node.keywords` sama sekali. Diverifikasi: `open(x,
   "w")` → skor 15 (terdeteksi); `open(x, mode="w")` — OPERASI IDENTIK —
   → skor 0 (lolos total). Diperbaiki: cek posisional DAN keyword `mode=`.
4. **`GET /auth/callback` (OIDC) — perbandingan `state` bukan constant-time.**
   `state != cookie_state` (operator biasa) dipakai untuk membandingkan token
   anti-CSRF acak, TIDAK konsisten dengan pola timing-safe yang SUDAH
   ditegakkan di seluruh perbandingan sejenis lain di codebase ini (CSRF form
   token, login token — audit produksi 2026-07-29). Diperbaiki ke
   `hmac.compare_digest` untuk konsistensi standar keamanan proyek sendiri
   — risiko praktis rendah (flow login sekali-pakai, bukan endpoint yang
   dipanggil berulang), tapi tak ada alasan membiarkan satu pengecualian.

**Sudah solid (dibaca, tak perlu tindakan):** `security/policy_engine.py`
(fail-safe konsisten, `deny_if` menang atas `approval_required_if`),
`security/guardrails.py` (keterbatasan streaming sudah didokumentasikan
jujur, pola regex PII/leak konservatif by design), `security/oidc.py` sisanya
(signature/iss/aud/exp/nonce semua diverifikasi ketat, algoritma dibatasi
eksplisit `["RS256", "ES256"]` — cegah algorithm-confusion), `security/vault.py`
(env-var-backed cache tak pernah stale karena env tak berubah selama proses
hidup; enkripsi-at-rest `Fernet` sudah diaudit sesi sebelumnya).

Diverifikasi via `uv run --python 3.12`: **998 passed** (+6 test regresi
baru: `tests/test_rate_limit.py`, `tests/test_auth_web.py`,
`tests/test_rbac_web.py` ×2, `tests/test_skill_scanner.py`), ruff
check/format bersih, tanpa dependency baru. Tiap bug diverifikasi GAGAL
lebih dulu terhadap kode SEBELUM perbaikan (bukan cuma lolos setelah
diperbaiki) — memastikan test benar-benar menangkap regresi, bukan
kebetulan lolos.

---

## 11. Audit lapisan `infra/` — internal modul (2026-09-01)

Babak audit lanjutan (setelah §10 menyelesaikan `security/`), atas
permintaan eksplisit owner. Fokus: `infra/users.py`, `infra/config.py`,
`infra/chat_sessions.py`, `infra/settings.py`, `infra/manifest.py`,
`infra/backup.py`, `infra/env.py` — fondasi yang dipakai semua modul lain,
belum pernah diaudit langsung sesi-sesi sebelumnya. Metodologi sama: baca
kode + reproduksi terisolasi sebelum menindaklanjuti.

**Diperbaiki:**

1. **`UserStore.upsert_on_login` — race condition TOCTOU pada bootstrap
   admin pertama.** Versi lama: `SELECT COUNT(*)` (cek "apakah tenant ini
   sudah punya user") lalu, TERPISAH, `INSERT` dengan role yang sudah
   diputuskan di sisi Python — ada jeda `await` di antara keduanya tempat
   scheduler asyncio bisa menyisipkan request LAIN. Diverifikasi lewat
   reproduksi terisolasi: dua `upsert_on_login()` untuk subject BEDA
   dijalankan bersamaan (`asyncio.gather`) pada tenant kosong (persis
   kondisi dua user OIDC berbeda login hampir bersamaan saat instance BARU
   pertama kali di-setup) → **KEDUANYA jadi admin**, bukan cuma satu.
   Diperbaiki: `access_role` dihitung via subquery korelasi DI DALAM satu
   statement `INSERT` yang sama (`CASE WHEN (SELECT COUNT(*) ...) = 0 THEN
   'admin' ELSE 'member' END`) — atomik terhadap SQLite writer lock, tak ada
   jeda `await` Python di tengahnya untuk request lain menyisip.

**Sudah solid (dibaca, tak perlu tindakan):** `infra/config.py` (deklaratif,
tak ada logika runtime kompleks di luar `from_env()` yang sudah benar),
`infra/chat_sessions.py` (isolasi tenant/owner konsisten dengan pola yang
sudah diverifikasi §6), `infra/settings.py` (fail-safe ke default untuk
nilai tak dikenal, konsisten di semua getter), `infra/manifest.py`
(`role`/`roles_dir` dari `clawn.yaml` bisa secara teoretis path-traversal,
TAPI hanya dipanggil dari `scripts/apply_manifest.py` — CLI lokal yang
dijalankan operator sendiri di mesinnya sendiri, bukan endpoint remote;
tak melintasi batas privilese apa pun), `infra/backup.py` (blocking
`sqlite3` sinkron sengaja — hanya dipanggil dari `scripts/backup_db.py`,
CLI standalone di luar event loop async, bukan dari `web/main.py`),
`infra/env.py` (parser `.env` minimal, tanpa eksekusi shell/injeksi).

Diverifikasi via `uv run --python 3.12`: **999 passed** (+1 test regresi
baru: `tests/test_users.py::test_concurrent_first_logins_bootstrap_only_one_admin`),
ruff check/format bersih, tanpa dependency baru. Bug diverifikasi GAGAL
lebih dulu terhadap kode lama (`asyncio.gather` dua login pertama →
`['admin', 'admin']`) sebelum diperbaiki (→ `['admin', 'member']`).

---

## Sumber riset tren (dicari 2026-07-27)

- [The best AI agent frameworks in 2026](https://www.langchain.com/resources/ai-agent-frameworks)
- [AI Agent Protocols 2026: The Complete Guide to Standardizing AI Communication](https://www.ruh.ai/blogs/ai-agent-protocols-2026-complete-guide)
- [AI Agent Orchestration Goes Enterprise: The April 2026 Playbook](https://www.fifthrow.com/blog/ai-agent-orchestration-goes-enterprise-the-april-2026-playbook-for-systematic-innovation-risk-and-value-at-scale)
- [AI Agent Observability & Governance: 2026 Market Reality](https://guptadeepak.com/ai-agent-observability-evaluation-governance-the-2026-market-reality-check/)
- [Agentic AI Enterprise Adoption 2026: Governance Gap](https://agenticaiinstitute.org/agentic-ai-enterprise-adoption-2026-governance-gap/)
- [State of AI Agents 2026: Lessons on Governance, Evaluation and Scale](https://lovelytics.com/post/state-of-ai-agents-2026-lessons-on-governance-evaluation-and-scale/)
- [Non-Human Identity Access Management Market 2026](https://www.grandviewresearch.com/industry-analysis/non-human-identity-access-management-market-report)
- [Agentic AI identity: A 6-stage maturity model for non-human identities](https://www.csoonline.com/article/4194548/agentic-ai-identity-a-6-stage-maturity-model-for-non-human-identities.html)
- [Agentic AI, non-human identities and the next era of IAM (SailPoint)](https://www.sailpoint.com/blog/agentic-ai-and-the-future-of-iam)
- [Hybrid Cloud-Local LLM: The Complete Architecture Guide (2026)](https://www.sitepoint.com/hybrid-cloudlocal-llm-the-complete-architecture-guide-2026/)
- [LLM Model Routing in 2026: Cost-Quality Optimization](https://www.digitalapplied.com/blog/llm-model-routing-2026-cost-quality-optimization-engineering-guide)
- [Self-Evolving Agents: Real Learning, or Memory in a Costume?](https://medium.com/@Micheal-Lanham/self-evolving-agents-real-learning-or-memory-in-a-costume-c397f46bbfce)
- [Always-On Agents: A Survey of Persistent Memory, State, and Governance in LLM Agents](https://arxiv.org/pdf/2606.30306)

## Sumber riset tren (dicari 2026-08-03, untuk §9)

**Regulasi & audit trail (§9.1):**
- [EU AI Act Article 12: What AI Teams Need to Log Before August 2026](https://aisecuritygateway.ai/blog/eu-ai-act-article-12-compliance-logging)
- [Article 12 and the Logging Mandate: What the EU AI Act Actually Requires (FireTail)](https://www.firetail.ai/blog/article-12-and-the-logging-mandate-what-the-eu-ai-act-actually-requires)
- [What the EU AI Act requires for AI agent logging (Help Net Security)](https://www.helpnetsecurity.com/2026/04/16/eu-ai-act-logging-requirements/)
- [EU AI Act Articles 12 & 13: Decision Traceability & Audit Compliance](https://aigovernancedesk.com/eu-ai-act-articles-12-13-decision-traceability/)
- [draft-sharif-agent-audit-trail-00 (IETF Internet-Draft — individual submission, BUKAN WG doc, expired 2026-09-29)](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/)

**Non-Human Identity (§9.2):**
- [AI Agents: The Next Wave Identity Dark Matter (The Hacker News)](https://thehackernews.com/2026/03/ai-agents-next-wave-identity-dark.html)
- [AI agent identity security in 2026: are your controls keeping up? (NHI Mgmt Group)](https://nhimg.org/community/agentic-ai-and-nhis/ai-agent-identity-security-in-2026-are-your-controls-keeping-up/)
- [Top non-human identity (NHI) management tools for enterprise (One Identity)](https://www.oneidentity.com/learn/top-non-human-identity-and-agentic-ai-security-tools.aspx)

**OpenTelemetry GenAI (§9.3 — dasar rekomendasi "tunggu dulu"):**
- [OpenTelemetry's GenAI semantic conventions are NOT stable yet — what actually shipped in 2026](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke)
- [How OpenTelemetry Traces LLM Calls, Agent Reasoning, and MCP Tools (Greptime)](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)
- [OpenTelemetry for AI Agents: Observability, Tracing, and the GenAI Semantic Conventions (Zylos)](https://zylos.ai/research/2026-02-28-opentelemetry-ai-agent-observability/)

**Routing cost-quality (§9.4):**
- [Local LLMs vs Cloud APIs: 2026 Total Cost of Ownership Analysis (SitePoint)](https://www.sitepoint.com/local-llms-vs-cloud-api-cost-analysis-2026/)
- [Hybrid AI Architecture: Routing Models to Reduce Cost Without Reducing Quality (Princeton IT)](https://princetonits.com/blog/artificial-intelligence-ai/hybrid-ai-architecture-part-2-routing-models-to-reduce-cost-without-reducing-quality/)
