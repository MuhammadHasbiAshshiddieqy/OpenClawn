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

## 12. Task Graph — DAG subtask + concurrency engine (2026-09-09)

Dikerjakan atas permintaan eksplisit owner setelah membaca
`IMPROVEMENT-Sandbox-Isolation-Parallelization.md` (dokumen eksternal, tak
tracked git, dijatuhkan ke repo — bukan berasal dari audit internal proyek
ini). Proposal itu mengusulkan arsitektur ala Manus: DAG subtask + eksekusi
paralel, sandbox-per-subtask dengan lifecycle sleep/wake/recycle, fault
containment, context hygiene, observability/replay, dan runtime isolasi
pluggable (gVisor/Firecracker). Arahan owner eksplisit: **"tidak masalah
kalau sudah tidak minimalis, utamakan fungsi dan kebutuhan"** — CLAUDE.md §8
minimalis DILONGGARKAN untuk inisiatif ini, TAPI ranking keamanan #1 CLAUDE.md
TIDAK dilonggarkan (trade-off keamanan apa pun tetap butuh persetujuan
eksplisit terpisah, bukan keputusan sepihak).

**Keputusan skop (via `EnterPlanMode`/`ExitPlanMode`, disetujui owner
eksplisit sebelum kode ditulis):** dari 5 fase yang diusulkan, owner memilih
**HANYA Fase 1 (DAG model + orchestrator) dan Fase 2 (concurrency engine +
fault containment + audit threading)** untuk dikerjakan sekarang, dengan
**submission EKSPLISIT** (bukan auto-decompose LLM). Fase 3 (sandbox
lifecycle — trade-off keamanan nyata: filesystem persisten vs ephemeral
`--rm` saat ini), Fase 4 (observability/replay endpoint), dan Fase 5 (runtime
pluggable gVisor/Firecracker) **DITUNDA**, dicatat sebagai non-goal eksplisit
di `docs/core.md` § Task Graph — bukan lupa.

**Riset sebelum desain (2 Explore agent + 1 Plan agent, dibaca & diverifikasi
langsung ke kode, bukan diasumsikan):** dikonfirmasi TIDAK ADA machinery
DAG/paralel/worker-pool apa pun di seluruh codebase sebelumnya —
`core/conversation.py` (multi-agent) strictly sekuensial; `core/autopilot.py`
strictly sekuensial, TANPA retry/circuit-breaker; `tools/todo.py` daftar
linear tanpa dependency. Ini benar-benar wilayah baru, bukan perluasan
sesuatu yang sudah setengah ada.

**Hasil:**
- `core/task_graph.py` (baru) — `TaskNode`/`TaskGraph` MURNI (tanpa I/O/DB/
  LLM): `validate()` (node_id duplikat, `depends_on` tak dikenal, role tak
  dikenal — cek `roles/<role>/soul.toml` ada, SAMA pola `infra/manifest.py`),
  `detect_cycle()` (DFS 3-warna), `ready_nodes()`, `transitive_dependents()`.
  **Fail-closed**: graph malformed/cyclic ditolak SELURUHNYA sebelum satu
  subtask pun mulai.
- `core/task_executor.py` (baru) — `TaskGraphExecutor`, penjadwalan
  EVENT-DRIVEN (bukan "wave" tetap — node cepat tak menunggu sibling lambat
  yang tak jadi dependency-nya), dibatasi `task_graph_max_concurrency`.
  Fault containment STRUKTURAL (try/except DI TITIK EKSEKUSI `_run_node`,
  bukan hanya `return_exceptions=True` di titik agregasi) — satu node
  meledak TAK PERNAH menjalar ke `asyncio.wait`, mustahil membatalkan
  sibling. Retry+backoff eksponensial sampai `task_graph_max_node_attempts`
  (INI SEKALIGUS breaker-nya, tanpa abstraksi circuit-breaker terpisah).
  Node gagal permanen → `transitive_dependents()`-nya `blocked`, node
  independen lain TETAP jalan — graph `status="partial"` dihargai sebagai
  hasil nyata, bukan dibuang jadi "failed" total.
- **`autopilot=True` WAJIB (bukan opsional) untuk tiap subtask** — pelajaran
  LANGSUNG dari bug `scripts/run_evals.py` (§ Prioritas 8.2): subtask di sini
  tak punya listener SSE/UI, `request()` biasa akan menggantung sampai
  timeout tanpa siapa pun pernah melihat kartu approval-nya. Dengan
  `autopilot=True`, tool butuh-approval diantri sebagai proposal
  (`queue_proposal`, sudah terpasang `_execute_tool`) — tercatat, ditinjau
  lewat `GET /approvals` nanti (termasuk orphan-cleanup lintas restart dari
  § Prioritas 8.1), TIDAK menggantung graph.
- Tool baru `task_graph_submit` (`tools/task_graph_submit.py`) —
  `requires_approval=False` (tool ini sendiri hanya mengorkestrasi turn agent
  lain; aksi destruktif subtask tetap digerbangi individual). Validasi
  SEBELUM DB/eksekusi disentuh (semua-atau-tidak). Diizinkan role
  `pm`/`dev`/`qa`/`data` (yang sudah punya `todo_write`), BUKAN `security`
  (read-only).
- `task_id`/`node_id` di-thread sebagai kwarg OPSIONAL (pola SAMA
  `agent_identity`, tak pernah wajib) lewat `AgentConfig` →
  `RoutingAuditor.log_decision()` → `ApprovalGate.request()`/`auto_approve()`/
  `queue_proposal()` → `ToolAudit.record()`. Kolom nullable baru di
  `routing_events`/`approval_log`/`tool_invocations` (dual-listed: `CREATE
  TABLE` untuk DB baru + `_ADDED_COLUMNS` untuk DB lama, pola sama
  `agent_identity`). Tabel baru `task_graphs`+`task_nodes`.
- Impor sirkular ditemukan & diperbaiki SEBELUM commit (bukan setelah error
  produksi): `core.agent_loop` mengimpor `tools` (untuk `TOOL_REGISTRY`)
  SEBELUM class `AgentLoop` didefinisikan — `tools/task_graph_submit.py`
  butuh `AgentLoop`/`TaskGraphExecutor` (yang keduanya mengimpor
  `core.agent_loop`), jadi impor level-modul akan memicu "partially
  initialized module". Diperbaiki: impor `TaskGraphExecutor`/`AgentLoop`
  LOKAL di dalam `execute()`, bukan level-modul.

**Non-goal eksplisit versi ini** (dicatat `docs/core.md`, bukan lupa):
auto-decompose LLM, sandbox lifecycle (Fase 3), runtime isolasi pluggable
gVisor/Firecracker (Fase 5).

Diverifikasi via `uv run --python 3.12`: **1038 passed** (+39 test baru:
`tests/test_task_graph.py` ×12, `tests/test_task_executor.py` ×8,
`tests/test_task_graph_submit.py` ×11, plus passthrough `task_id`/`node_id`
di `tests/test_audit.py`/`tests/test_security.py`/`tests/test_tools.py`),
ruff check/format bersih, tanpa dependency baru. Dua bug ditemukan &
diperbaiki lewat test yang GAGAL lebih dulu sebelum kode ditulis benar
(bukan lolos kebetulan): (1) `TaskGraph.__init__`'s dict comprehension
menimpa `node_id` duplikat diam-diam sebelum `validate()` sempat melihatnya
— dipindah ke cek eksplisit di `__init__` SEBELUM dict dibangun; (2) test
`ready_nodes()` awal salah asumsi (memberi id "completed" tanpa mengubah
`node.status` node itu sendiri) — diperbaiki jadi konsisten dengan kontrak
nyata `ready_nodes(completed)` (mengasumsikan `completed` SELALU sinkron
dengan `node.status`, sama seperti pemakaian nyata di `TaskGraphExecutor`).

**Susulan (2026-09-09): Fase 4 (observability/replay) — ✅ SELESAI.** Dipilih
lanjut karena EKSPLISIT rendah-risiko (murni baca data yang sudah ada dari
Fase 1+2, tak menyentuh model sandbox/keamanan sama sekali) — beda dari Fase
3 yang butuh persetujuan trade-off keamanan terpisah sebelum kode ditulis
(TETAP ditunda). `GET /tasks/{task_id}` (`web/main.py`) mengembalikan baris
`task_graphs` + seluruh `task_nodes`-nya; `GET /tasks/{task_id}/timeline`
menggabungkan `routing_events`+`tool_invocations`+`approval_log` (filter
`task_id`, diurut `created_at`) jadi satu daftar "replay" lintas node — pola
sama `GET /evidence/{event_id}` yang sudah ada. Kepemilikan (`task_graphs.
owner_user_id`) digerbangi SEJAK endpoint ini pertama kali dibuat (bukan gap
yang ditambal belakangan seperti beberapa kasus lain di riwayat proyek ini) —
pola sama `GET /chat-sessions/{id}/turns` (`_can_access_owned_resource`).
Tak ada migrasi/kolom baru — semua yang dibutuhkan sudah ada dari Fase 1+2.

Diverifikasi via `uv run --python 3.12`: **1047 passed** (+9: 5 di
`tests/test_task_graph_web.py` untuk bentuk response/join timeline, 4 di
`tests/test_rbac_web.py` untuk isolasi kepemilikan lintas-user — member
ditolak baca task/timeline user lain, member tetap baca task sendiri, admin
tetap baca task siapa pun), ruff check/format bersih, tanpa dependency baru.

**Susulan (2026-09-15): Fase 5 (runtime isolasi pluggable) — ✅ SELESAI,
Fase 3 (sandbox lifecycle) TETAP DITUNDA.** Ditanya eksplisit lewat
`AskUserQuestion`: Fase 3 (butuh persetujuan trade-off keamanan) vs Fase 5
(rendah-risiko, satu config knob) vs berhenti — owner memilih Fase 5.

`AppConfig.sandbox_runtime: str = "runc"` (baru, `infra/config.py`, env
`OPENCLAWN_SANDBOX_RUNTIME`) — `tools/sandbox.py::_base_docker_args`
meneruskannya sebagai `--runtime <value>` ke `docker run` HANYA bila NON-default
(mis. `"runsc"` untuk [gVisor](https://gvisor.dev/), isolasi kernel-level
lebih kuat) — default `"runc"` tak pernah menyentuh argv sama sekali,
perilaku lama utuh. **Satu flag Docker, bukan perubahan arsitektur** — tak
ada abstraksi "pluggable backend"/plugin interface yang dibangun (tak
dibutuhkan untuk satu flag). Kode ini TIDAK memverifikasi runtime itu benar-
benar terpasang di Docker daemon operator — tanggung jawab operator (`docker
info --format '{{.Runtimes}}'`). Berlaku HANYA `run_python`/`run_shell`
(eksekusi kode) — `build_project_image` (§ Prioritas 8.3) sengaja tak
disentuh, beda skop (network sengaja terbuka SAAT build).

**Firecracker/microVM TETAP direkomendasikan dilewati** (bukan bagian dari
Fase 5 yang dikerjakan) — bukan `docker run --runtime` biasa, butuh stack
orkestrasi terpisah (`firecracker-containerd`/Kata/Ignite) yang sering tak
tersedia di VPS self-host tanpa nested virtualization. Dicatat sebagai
backlog untuk deployment enterprise/multi-tenant masa depan, bukan lupa —
bisa masuk lewat knob `sandbox_runtime` yang SAMA nanti bila runtime
container-nya sendiri diganti (mis. `sandbox_runtime="kata"`).

**Susulan (2026-09-15): Fase 3 (sandbox lifecycle) — ✅ SELESAI.** Fase yang
sengaja ditunda paling akhir karena satu-satunya yang melonggarkan trade-off
keamanan nyata. Ditanya eksplisit lewat `AskUserQuestion` (dua putaran):
"bangun sekarang, opt-in per sesi" (bukan default-on, bukan ditunda lagi), lalu
"tersedia untuk semua role dengan `code_run`" (`dev`/`qa`/`data` — BUKAN
`pm`/`security` yang tak punya `code_run` sama sekali). Direncanakan penuh
lewat `EnterPlanMode`/`ExitPlanMode` (riset fakta-dulu: dikonfirmasi tak ada
`docker.sock` mount/`DOCKER_HOST` di repo, jadi `docker run -d`/`exec`/
`pause`/named volume semua terjangkau via CLI host yang sama seperti `docker
run --rm` sebelumnya — nol infra baru) sebelum satu baris kode ditulis.

**Trade-off keamanan yang diterima, jujur dicatat:** sebelumnya SETIAP
`code_run` = `docker run --rm` + temp-dir sekali pakai — state (file, package
terinstall) hilang total begitu container keluar, tak ada jejak yang bisa
bertahan lintas panggilan. Sekarang, sesi yang secara EKSPLISIT memanggil
tool baru `sandbox_persist_enable` mendapat container `docker run -d` +
named Docker volume untuk `/work` yang bertahan lintas panggilan `code_run`
BERIKUTNYA dalam sesi yang sama — kode berbahaya di satu panggilan BISA
meninggalkan jejak yang bertahan sampai container di-recycle. `--network
none`, `--read-only` (kecuali `/work`), non-root, `no-new-privileges` TETAP
tak berubah — HANYA persistensi filesystem yang dilonggarkan, dan HANYA untuk
sesi yang secara sadar memilihnya (opt-in, bukan default).

**Keputusan skop dibuat transparan saat planning (bukan ditanyakan terpisah,
dinilai cukup rendah-risiko untuk diputuskan langsung — dicatat di plan agar
owner bisa menantang sebelum `ExitPlanMode` disetujui):** persistensi HANYA
untuk `code_run`, TIDAK PERNAH `shell_run`. `shell_run` ada murni untuk
inspeksi workspace ASLI read-only (grep/find/git log); `code_run` sudah sama
sekali tak pernah mount workspace asli (kode ditulis ke temp-dir sekali
pakai) — mengganti mount itu dengan volume persisten adalah perubahan
mandiri, mencampurnya ke `shell_run` akan mencampur dua concern tak
berhubungan tanpa manfaat.

**Hasil:**
- `infra/sandbox_lifecycle.py` (baru) — `CURRENT_PERSISTENT_SANDBOX`
  (ContextVar) + `effective_persistent_container()` + `SessionSandboxContainerStore`
  (CRUD `session_sandbox_container`, termasuk `count_active()` untuk batas
  DoS). Pola SAMA PERSIS `infra/sandbox_image.py`
  (`CURRENT_SANDBOX_IMAGE`/`SessionSandboxImageStore`) — ContextVar dipulihkan
  `AgentLoop.run()` dari DB tiap turn, direset di `finally`, bertahan lintas
  turn DAN restart server (container Docker & baris SQLite sama-sama
  bertahan lintas restart proses app).
- Tabel baru `session_sandbox_container` (`session_id` PK, `container_id`,
  `volume_name`, `state` running/paused, `created_at`, `last_used_at`) —
  state operasional MURNI (baris DIHAPUS begitu container di-destroy, bukan
  disimpan `state='destroyed'`).
- `tools/sandbox.py::DockerSandbox` — 6 method baru: `create_persistent`
  (`docker run -d`, nama container/volume DETERMINISTIK dari hash
  `session_id` — idempoten, hindari parsing stdout `docker run` untuk id),
  `exec_persistent` (kode ditulis via **stdin**, `docker exec -i ... cat >
  script.py` — bukan diinterpolasi ke argumen shell, nol risiko shell
  injection dari isi kode), `pause_persistent`/`resume_persistent` (`docker
  pause`/`unpause`), `_run_lifecycle_command` (helper bersama), dan
  `destroy_persistent` (`docker rm -f` + `docker volume rm`, FAIL-SOFT
  sepenuhnya — pembersihan tak boleh diblokir container setengah rusak).
  `run_python` gains satu branch di awal: `effective_persistent_container()`
  terisi → delegasi ke `exec_persistent`, kalau tidak jalur ephemeral lama
  byte-identik.
- Tool baru `sandbox_persist_enable` (`tools/sandbox_persist.py`) —
  `requires_approval=True` **selalu**, masuk `_TRUST_MODE_EXEMPT` (sekelas
  `build_sandbox_image`, malah lebih sensitif: state writable yang bertahan
  lintas panggilan, bukan cuma network sesaat saat build). Idempoten (sesi
  yang sudah opt-in → no-op sukses, bukan container kedua). Diizinkan role
  `dev`/`qa`/`data` (yang sudah punya `code_run`), BUKAN `pm`/`security`.
- `AppConfig.sandbox_persist_max_containers` (default 5) — batas DoS baru
  yang model ephemeral lama TAK PUNYA (banyak sesi opt-in sekaligus bisa
  membebani host tanpa batas), dicek `sandbox_persist_enable` SEBELUM
  membuat container baru.
- `core/sandbox_reaper.py::SandboxReaper` (baru) — bentuk SAMA PERSIS
  `AutopilotScheduler` (`start()`/`stop()`/`_loop()`/`run_due_once()` yang
  testable tanpa tick nyata, `add_done_callback` crash logger). Idle >
  `sandbox_persist_idle_ttl_sec` (600s) → `pause_persistent`; tak dipakai >
  `sandbox_persist_destroy_ttl_sec` (3600s, dicek TERLEPAS state) →
  `destroy_persistent` + hapus baris DB permanen. Dipasang `web/main.py`
  lifespan persis seperti `autopilot_scheduler`.
- `AgentLoop.run()` — satu blok ContextVar restore lagi (bentuk SAMA PERSIS
  dua blok sebelumnya untuk workspace/sandbox-image): muat
  `session_sandbox_container`, `touch()` (tandai dipakai turn ini — dasar
  keputusan idle reaper), auto-`resume_persistent` bila `paused` (transparan
  bagi model, tak perlu tool "resume" terpisah), set
  `CURRENT_PERSISTENT_SANDBOX`.

**Dengan ini, proposal `IMPROVEMENT-Sandbox-Isolation-Parallelization.md`
SELESAI diproses SEPENUHNYA**: semua 5 fase (DAG, concurrency, sandbox
lifecycle, observability/replay, runtime pluggable) sudah dikerjakan atas
keputusan eksplisit owner di tiap titik trade-off.

Diverifikasi via `uv run --python 3.12`: **1084 passed** (+35: 8 di
`tests/test_sandbox_lifecycle.py` untuk `SessionSandboxContainerStore` CRUD
murni, 10 di `tests/test_sandbox_persist_tool.py` untuk tool (idempoten, cap
enforcement, role allow-list dev/qa/data vs pm/security), 6 di
`tests/test_sandbox_reaper.py` untuk pause/destroy/untouched TTL logic, 8
argv-verification baru di `tests/test_tools.py` untuk `DockerSandbox`
persistent methods (flag keamanan wajib tetap ada di `docker run -d`, kode
via stdin bukan argv, delegasi `run_python`↔`exec_persistent`), 1 di
`tests/test_trust_mode.py` untuk `_TRUST_MODE_EXEMPT`, plus tool count
29→30), ruff check/format bersih, tanpa dependency baru.

---

## 13. Audit lapisan `core/` — internal modul (2026-09-15)

Babak audit lanjutan (setelah §10 `security/`, §11 `infra/`), atas
permintaan eksplisit owner. Fokus: seluruh `core/` (26 file, ~6200 baris) —
modul terbesar & paling sentral (agent loop, router, audit, crystallizer,
multi-agent conversation, LLM client, task graph, autopilot), belum pernah
diaudit langsung sesi-sesi sebelumnya. Metodologi sama §10/§11: baca kode +
reproduksi terisolasi SEBELUM menindaklanjuti. Dibagi dua jalur paralel:
`core/agent_loop.py` (file terbesar, paling sensitif keamanan) dibaca
langsung; 25 file `core/` lainnya disurvei agent riset terpisah (read-only,
tanpa edit) lalu tiap temuan diverifikasi ulang manual sebelum diperbaiki.

**Diperbaiki:**

1. **`AgentLoop.run()` — `SandboxUnavailable` tak tertangkap membuat sesi
   dengan sandbox persisten `paused` crash total & permanen begitu Docker
   tak tersedia.** Blok auto-resume sandbox persisten (§ Prioritas 12 Fase
   3, ditambahkan sesi ini juga) memanggil `resume_persistent` SEBELUM
   `try/finally` yang mereset ContextVar sempat mulai — Docker yang benar-
   benar tak terpasang/daemon mati membuat `SandboxUnavailable` RAISE
   (bukan return dict error), menjatuhkan SELURUH turn sebelum LLM sempat
   dipanggil sama sekali (tak ada routing/audit/jawaban). Baris DB
   `state='paused'` tak pernah berubah, jadi SETIAP turn berikutnya untuk
   sesi itu gagal identik — rusak permanen tanpa jalan pulih sampai operator
   turun tangan manual. Diverifikasi GAGAL dulu (reproduksi terisolasi:
   sesi dengan container `paused` + Docker di-mock hilang → `AgentLoop.run()`
   crash) sebelum diperbaiki: seluruh blok restore/resume dibungkus
   try/except (pola sama `_maybe_compact`/`_generate_session_title` yang
   sudah fail-safe di file yang sama) — gagal → log lalu turn jatuh ke
   jalur ephemeral, bukan menjatuhkan turn. 4 test regresi baru
   (`tests/test_sandbox_lifecycle.py`).
2. **SSRF guard bisa dilewati via HTTP redirect** — `web_fetch`,
   `http_request` (`tools/web.py`), dan `SkillPack.import_url`
   (`core/skill_pack.py`) memvalidasi host lewat `_ssrf_guard` SEBELUM
   request, lalu memakai `httpx.AsyncClient(follow_redirects=True)` — httpx
   mengikuti redirect SENDIRI tanpa re-validasi apa pun, jadi URL publik
   yang membalas 3xx ke host internal (metadata cloud, `localhost`,
   RFC1918) lolos sepenuhnya karena guard hanya pernah melihat URL AWAL.
   Diperbaiki: `_stream_capped` (dipakai bersama ketiga jalur) sekarang
   mengikuti redirect MANUAL, me-re-validasi `_ssrf_guard` tiap hop
   `Location` SEBELUM diikuti, dibatasi 5 hop. 11 test baru + 1 mock lama
   (`test_skill_scanner.py`) yang jadi usang diperbaiki mengikuti bentuk
   argv baru.
3. **`ConversationOrchestrator._run_agent_turn` — deadlock permanen bila
   giliran agent meledak di tengah stream.** Sentinel penanda selesai
   (`queue.put(None)`) hanya dikirim di akhir jalur SUKSES — exception
   apa pun dari `agent.run()` (pipeline penuh: LLM, tool, DB, sandbox)
   membuat method berhenti tanpa mengirim sentinel, dan `run()` menunggu
   `queue.get()` yang tak pernah datang: HANG SELAMANYA, bukan error yang
   dilaporkan. Task pembawa exception itu sendiri juga tak pernah di-`await`
   siapa pun (macet di `queue.get()` sebelum sempat `await run_task`), jadi
   gagal sepenuhnya senyap. Diverifikasi GAGAL dulu (reproduksi terisolasi:
   `FakeAgent` yang raise di tengah `run()` → `orch.run()` tak selesai dalam
   5 detik) sebelum diperbaiki: sentinel dipindah ke `finally` (SELALU
   terkirim), exception re-raise natural lewat `await run_task` di `run()`
   — `web/main.py` sudah membungkus `orch.run()` dengan try/except yang
   melaporkannya ke UI (pola sama chat single-agent). 2 test regresi baru
   (`tests/test_conversation.py`).

**Ditemukan, diverifikasi, TAPI tidak perlu tindakan kode** (sudah
diputuskan/diterima sebelumnya atau sudah punya jaring pengaman):

- **TOCTOU di `security/approval.py::finalize_orphan`** (approval yatim
  bisa dieksekusi dua kali bila dua request approve bersamaan lolos cek
  `decision='pending'` sebelum salah satunya sempat menulis keputusan) —
  docstring method itu SENDIRI sudah secara eksplisit menyebut ini
  "risiko fail-soft yang diterima, bukan double-execute yang disengaja"
  (keputusan TODO.md § Prioritas 8.1). Tak ada temuan baru di luar yang
  sudah dipertimbangkan; dicatat di sini agar owner tahu masih berlaku.
- **`core/crystallizer.py` tak menerima `tenant_id`** (skill hasil
  crystallization selalu masuk tenant `'default'`, tak peduli tenant
  sesi sebenarnya) — konsisten dengan status Multi-Tenant yang SUDAH
  didokumentasikan CLAUDE.md §7 sebagai "fondasi + bukti konsep"
  (`ChatSessionStore`/`SkillDecayManager` "wired penuh SEBAGAI bukti
  konsep", bukan klaim penegakan tenant di SETIAP modul). `AgentConfig`
  memang tak punya field `tenant_id` sama sekali hari ini — gap sistemik
  yang sudah diketahui, bukan regresi baru.
- **Router memakai tier Gemini (`gemini-2.5-flash`/`-pro`), bukan cuma
  Claude, untuk COMPLEX/CRITICAL** — sempat disalahpahami sebagai potensi
  drift dari CLAUDE.md §7 ("Claude untuk berat"), tapi ternyata ini
  bukan temuan baru: `EVALUATOR_FOR` SUDAH disinkronkan penuh dengan
  roster ini (blocker 2026-07-27/28, lihat §2), LENGKAP dengan fail-safe
  `verified=False` yang memaksa `draft` untuk generator model APA PUN di
  luar peta — drift roster di masa depan sudah gagal aman, bukan cuma
  hari ini.
- **`EventBus.events` (antrian replay in-memory) tak pernah otomatis
  di-drain** — awalnya terlihat seperti fitur setengah jadi/leak, tapi
  `Event`/`self.events` didokumentasikan EKSPLISIT sebagai jalur
  "replay/audit" opsional (dipakai manual, bukan otomatis) dan memang
  ADA test yang membacanya (`test_events_replayable_from_bus_queue`).
  Satu `EventBus` berumur satu `ConversationOrchestrator` (per-request),
  jadi pertumbuhannya dibatasi umur SATU percakapan, bukan leak
  seluruh-proses.

Diverifikasi via `uv run --python 3.12`: **1096 passed** (+12 dari 1084
sebelum audit ini: 4 di `test_sandbox_lifecycle.py` untuk bug #1, 4
redirect-guard di `test_tools.py` + 2 di `test_skill_pack.py` untuk bug #2,
2 di `test_conversation.py` untuk bug #3, plus perbaikan 1 mock usang di
`test_skill_scanner.py` yang menargetkan bentuk argv lama), ruff
check/format bersih, tanpa dependency baru. Ketiga bug diverifikasi GAGAL
lebih dulu terhadap kode lama sebelum diperbaiki — bukan lolos kebetulan.

---

## 14. Re-audit `web/main.py` — endpoint yang tumbuh sejak §6 (2026-09-18)

Babak audit lanjutan (setelah §13 `core/` selesai), atas permintaan eksplisit
owner. `web/main.py` (2105 baris, seluruh HTTP/SSE endpoint) terakhir diaudit
khusus di §6 (2026-07-29), tapi sudah tumbuh besar sejak itu (RBAC, Task
Graph, MCP registry, sandbox reaper, percakapan multi-agent). Metodologi sama
§10/§11/§13: baca kode + reproduksi terisolasi SEBELUM menindaklanjuti —
kali ini file dibaca end-to-end oleh satu agent riset (read-only, tanpa
edit), dua temuan bahkan dikonfirmasi lewat reproduksi hidup via `TestClient`
sebelum dilaporkan; SETIAP temuan diverifikasi ulang manual terhadap kode
sungguhan sebelum diperbaiki — bukan diterima mentah-mentah.

**Diperbaiki (5 gap kepemilikan/RBAC nyata — SEMUA drift endpoint yang
ditambahkan BELAKANGAN dari pola yang sudah ditegakkan endpoint sejenis):**

1. **`/converse/stream` tak pernah mengisi `AgentConfig.user_id`** — approval
   dari tool butuh-approval di PERCAKAPAN MULTI-AGENT selalu tercatat
   `owner_user_id=None` di `approval_log`. Parah karena `pending_list()`
   (dipakai `GET /approvals`, polling normal UI) SENGAJA meloloskan baris
   tanpa owner ke SIAPA PUN yang login (fail-safe untuk resource lama tanpa
   owner tercatat) — jadi bukan cuma "butuh tebak approval_id", tapi
   LANGSUNG terlihat siapa pun yang polling `/approvals` biasa, lengkap
   `tool_input` (path/command/code) dan bisa langsung `POST /approve`-nya.
   Melumpuhkan HITL (§1) untuk SELURUH jalur multi-agent. Diperbaiki:
   `agent_factory` sekarang mengisi `user_id` dari `request.state.user`,
   pola SAMA `/chat/stream` (yang sudah benar sejak audit 2026-07-29).
2. **`/converse/interject` & `/converse/stop` tanpa gate kepemilikan sama
   sekali** — user login mana pun bisa menyuntik pesan palsu ke, atau
   menghentikan, percakapan multi-agent user lain hanya dengan
   menebak/mengetahui `session_id` (pola bug sama `/answer` sebelum diaudit
   2026-08-27). `ConversationControl` (extractable, web-agnostic per
   docstringnya) sengaja tak diberi field owner — kepemilikan dilacak
   `_conversation_owners` (dict in-memory BARU, pola sama `_conversations`
   sendiri) karena percakapan multi-agent (`persist_history=False`) tak
   punya baris `chat_sessions` untuk dijadikan sumber kepemilikan seperti
   `/answer`.
3. **`GET /approval/{approval_id}` tanpa `Request` param sama sekali** —
   beda dari `POST /approve` (digerbangi 2026-07-29), endpoint GET ini
   tak pernah membaca `owner_user_id` walau kolomnya sudah ada di
   `approval_log` sejak audit itu. Diperbaiki: tambah `request: Request` +
   `_can_access_owned_resource`.
4. **`GET /evidence/{event_id}` tanpa gate kepemilikan, DAN `event_id`
   integer autoincrement BERURUTAN** — jauh lebih parah dari #3: user login
   mana pun (termasuk role `viewer`) bisa mengiterasi `1, 2, 3, ...` untuk
   membaca evidence (policy/model/skill/guardrail) SELURUH sesi lintas
   tenant tanpa perlu menebak apa pun. `routing_events` tak punya kolom
   `owner_user_id` sendiri, tapi sudah punya `user_id` (`AgentConfig.user_id`)
   yang cukup untuk kepemilikan tanpa join tambahan — `"default"`
   diperlakukan sebagai "tak tercatat", pola sama `core/agent_loop.py`
   memperlakukan `user_id=="default"` sebagai `owner_user_id=None`.
5. **`POST /skills/apply-merge` & `POST /skills/revert-merge` tanpa
   `_require_role("admin")`** — drift dari `/skills/set-visibility`
   (digerbangi admin 2026-07-29, kelas mutasi SAMA: corpus skill bersama
   satu role). Diperbaiki: tambah gate identik.

**Diperbaiki (1 reliability, bukan security):**

6. **`_conversations`/`_conversation_owners` race pada `session_id` yang
   SAMA** — dua `/converse/stream` bersamaan dengan session_id sama (mis.
   `localStorage` dibagi dua tab) sebelumnya bisa membuat request yang
   selesai lebih dulu menghapus entri milik request LAIN yang masih
   berjalan (`pop()` tanpa cek identitas), mematikan diam-diam
   interject/stop untuk percakapan yang sebenarnya masih aktif. Diperbaiki:
   `finally` hanya menghapus bila registry masih menunjuk ke `control`
   instance milik request itu sendiri.

**Ditemukan, diverifikasi, TAPI SENGAJA tidak diperbaiki secara sepihak**
(butuh keputusan arsitektur owner, bukan bug sempit satu endpoint):

- **`GET /workspace/download` bisa "melewati" cek kepemilikan hanya dengan
  TIDAK mengirim `session_id`** — pada pandangan pertama terlihat seperti
  bug (parameter opsional yang menonaktifkan pemeriksaan keamanan), TAPI
  diverifikasi lebih dalam: `CONFIG.workspace_root` (folder default,
  dipakai SEMUA sesi yang tak pernah `set_workdir`) memang SATU folder
  BERSAMA di level tool juga — `tools/file_ops.py::FileReadTool` sendiri
  memakai `resolve_in_current_workspace(path, CONFIG.workspace_root)` TANPA
  isolasi per-user apa pun. Artinya: sesi mana pun yang memakai folder
  default SUDAH bisa saling `file_read` file satu sama lain lewat chat
  biasa, independen dari endpoint download ini — menambal HANYA endpoint
  download akan memberi rasa aman palsu (tool `file_read` tetap terbuka)
  sambil merusak backward-compat link lama. Ini gejala keputusan
  arsitektur "satu folder default dibagi semua sesi" yang sudah ada JAUH
  sebelum RBAC/multi-tenant, bukan regresi endpoint tunggal — dicatat di
  sini agar owner sadar & bisa memutuskan apakah workspace default perlu
  di-scope per-tenant/user (perubahan besar, di luar skop audit ini).

Diverifikasi via `uv run --python 3.12`: **1107 passed** (+11: 1 untuk bug
#1, 3 untuk bug #2, 2 untuk bug #3, 3 untuk bug #4 termasuk regresi negatif
"owner tak tercatat tetap terlihat", 2 untuk bug #5), ruff check/format
bersih, tanpa dependency baru. SEMUA 5 bug kepemilikan/RBAC diverifikasi
GAGAL lebih dulu (403 yang seharusnya muncul tapi tidak) terhadap kode lama
sebelum diperbaiki.

---

## 15. Audit `scripts/` — internal modul (2026-09-18)

Babak audit terakhir untuk melengkapi cakupan seluruh codebase (setelah
§6/§14 `web/`, §7 `memory/roles/tools/`, §10 `security/`, §11 `infra/`,
§13 `core/`). `scripts/` (6 file, 857 baris) belum pernah diaudit langsung.
Dibaca end-to-end sendiri (bukan agent riset — cukup kecil untuk dibaca
langsung), metodologi sama: cek race/TOCTOU, exception ditelan diam-diam,
resource leak, validasi input yang hilang.

**Hasil: bersih, tak ada temuan baru.** Semua 6 file adalah tool CLI yang
dijalankan operator sendiri di mesinnya (bukan endpoint web, tak melintasi
batas privilese apa pun — konsisten dengan penilaian `infra/manifest.py`/
`infra/backup.py` di §11):

- `apply_manifest.py`, `backup_db.py`, `anchor_audit_chain.py` — wrapper
  CLI tipis di atas `infra/manifest.py`/`infra/backup.py`/`core/audit_anchor.py`
  yang SUDAH diaudit (§11/§13), tanpa logika sendiri yang perlu ditinjau
  ulang.
- `route_sensitivity.py` — alat analisis MURNI baca (tanpa DB/network,
  memanggil API internal `SmartRouter` yang sengaja), tak ada permukaan
  keamanan sama sekali.
- `seed_routing.py` — hanya menulis ke path DB yang EKSPLISIT diberikan
  operator lewat `--db`, jelas dilabeli data sintetis di docstring & output
  CLI-nya sendiri, tak ada batas privilese yang dilintasi.
- `run_evals.py` — sudah membawa dua insiden nyata yang DIDOKUMENTASIKAN
  lengkap di komentar sendiri (`config=` yang tak diteruskan; DB ditutup
  sebelum background task `_post_turn` selesai, § Prioritas 8.2) — ditelusuri
  ulang logika task-tracking/cleanup-nya saat ini, tak ditemukan yang baru
  rusak.

Dengan ini, **seluruh folder kode OpenCLAWN (`core/`, `infra/`, `memory/`,
`roles/`, `security/`, `tools/`, `web/`, `scripts/`) sudah melalui minimal
satu babak audit dedicated** — bukan cuma disentuh insidental lewat
pengembangan fitur.

Tak ada perubahan kode/test (tak ada temuan untuk diperbaiki).

---

## 16. Audit frontend (JS/templates) — path traversal KRITIS ditemukan (2026-09-18)

Permintaan eksplisit owner: audit `web/static/*.js` (2290 baris —
`highlight.min.js` 1212 baris adalah vendor pihak ketiga, dilewati) dan
`web/templates/*.html` (14 file, 1420 baris), satu-satunya lapisan yang
belum pernah diaudit sama sekali (semua audit sebelumnya §6/§7/§10/§11/§13/
§14/§15 murni Python sisi server).

**`chat.js` (1078 baris, dibaca penuh) — bersih dari XSS nyata:**
`renderMarkdown()` memakai `marked` + `DOMPurify.sanitize()` (bukan
`innerHTML` mentah dari teks LLM), keduanya dimuat dari CDN dengan versi
terkunci + hash SRI (`marked@12.0.2`, `dompurify@3.1.6`) — tak bisa
disusupi diam-diam via CDN compromise. `escapeHtml()` cukup untuk SEMUA
konteks teks-node; satu titik di mana hasilnya disisipkan ke dalam atribut
`title="..."` (riwayat chat, nilai `role`) TIDAK menghadapi risiko nyata
lagi setelah perbaikan §16 di bawah (nilai `role` sekarang selalu anggota
`available_roles()`, bukan lagi string bebas).

**`web/templates/*.html` — bersih.** SEMUA pemakaian filter Jinja2 `|safe`
(11 titik, `_sidebar.html`/`autopilots.html`/`router.html`/`settings.html`/
`mcp.html`/`skills.html`) HANYA membungkus string dari `t(...)` (kamus i18n
`infra/i18n.py`, hardcoded developer, bukan data user) atau literal HTML
tetap (`'aria-current="page"'`) — tak ada satu pun yang mengalirkan data
user/LLM lewat `|safe`. `t()` sendiri memakai `str.format(**kwargs)` di
atas template STATIS; argumen yang disuntik (`tier`, `model`, `threshold`)
juga selalu nilai tetap/config, bukan input user.

### 🔴 KRITIS DITEMUKAN (bukan dari file JS/template, tapi DILACAK dari sana):
**path traversal via `role` → soul.toml arbitrer → privilege escalation total**

Menelusuri KE MANA field `role` yang dikirim `chat.js` (form
`/chat/stream`, `/converse/stream`) berakhir di sisi server menemukan:
`role` dipakai MENTAH untuk membangun path filesystem
(`f"roles/{role}/soul.toml"`) di **EMPAT** tempat — `core/agent_loop.py`,
`core/router.py`, `core/late_execute.py`, `core/task_graph.py` — TANPA
validasi apa pun. Diverifikasi lewat reproduksi terisolasi SEBELUM
diperbaiki: `AgentLoop(AgentConfig(role="../../../../../../tmp/pwn_dir"))`
SUKSES memuat `soul.toml` bikinan sendiri di luar `roles/`, lengkap
`[tools] allowed = ["code_run", "shell_run", "file_write", "http_request"]`
— privilege escalation TOTAL yang membypass SELURUH model permission
berbasis soul.toml/RBAC, reachable dari `POST /chat/stream`/
`POST /converse/stream` TANPA login sama sekali (auth default OFF).
`core/task_graph.py::TaskGraph.validate()` punya cek, tapi LEMAH
(`Path(...).exists()` — tetap `True` untuk traversal yang benar-benar
berujung ke `soul.toml`, tak mencegah traversal itu sendiri).

**Diperbaiki:** `roles/registry.py::available_roles()` (baru) — validator
keanggotaan-set yang aman (glob SATU level `roles/`, hasil selalu nama
folder MURNI, tak pernah mengandung `/` atau `..`). Diterapkan independen
di KEEMPAT titik rentan (defense-in-depth, pola sama `_TRUST_MODE_EXEMPT`)
plus dua entry point web (`/chat/stream`'s `role`, `/converse/stream`'s
`participants` CSV) agar client tak sah dapat error jelas, bukan 500
mentah. `SmartRouter`'s parameter `soul_path` eksplisit (dipakai luas oleh
test suite router sendiri) TETAP dilewati — caller yang secara sadar
meneruskan path bukan target celah ini.

Diverifikasi via `uv run --python 3.12`: **1118 passed** (+11: 9 di
`tests/test_role_validation.py` baru — mencakup `available_roles()` dan
KEEMPAT titik pertahanan independen — plus 2 di `tests/test_web.py` untuk
respons lapisan web), ruff check/format bersih, tanpa dependency baru.
Kerentanan diverifikasi GAGAL (soul.toml arbitrer termuat) terhadap kode
lama sebelum diperbaiki, lalu diverifikasi BLOCKED setelahnya — bukan lolos
kebetulan.

---

## 17. Audit menyeluruh lintas-lapisan (2026-09-25)

Permintaan owner: "audit keseluruhan code, sepertinya masih banyak
kekurangan", lalu "perbaiki semuanya, ikuti urutan rekomendasi". Beda dari
§6-§16 (per lapisan): fokus ke **sambungan antar-modul** — celah yang tak
terlihat bila tiap lapisan diaudit terpisah. Metodologi sama: baca kode +
reproduksi terisolasi SEBELUM memperbaiki; skrip reproduksi yang sama
dijalankan ulang setelah perbaikan (semuanya tertutup).

### 🔴 Kritis — kebocoran data/credential
1. **`set_workdir`/field `workdir` menerima folder APA PUN (termasuk `/`)** —
   tool tanpa approval → `file_read` bisa membaca `/proc/self/environ` (semua
   API key + `OPENCLAWN_ENCRYPTION_KEY`), `/workspace/download` bisa mengunduh
   DB seluruh tenant. Direproduksi. **Diperbaiki:** allowlist root
   (`OPENCLAWN_WORKDIR_ROOTS`; default home+workspace tanpa auth, KOSONG saat
   auth aktif; hanya admin), divalidasi ulang di `AgentLoop.run()` dan
   `/workspace/download` (baris lama `session_workspace` di luar allowlist
   diabaikan). *Keputusan owner:* mengikuti rekomendasi audit.
2. **Memori L1/L4 bocor antar user** — checkpoint `last_summary` satu baris
   per ROLE (jawaban user A disuntik ke prompt user B tiap turn), arsip L4
   dicari lintas semua sesi. **Diperbaiki:** L1 per sesi, L4 per pemilik
   sesi (`chat_sessions.owner_user_id`); `memory_search` ikut difilter.
3. **Rantai curi credential tanpa approval** — workspace default `.` berisi
   `.env`; `file_read` + `web_fetch` keduanya tanpa approval. `http_request`
   me-resolve `vault:<env apa pun>`, dan trust mode melewatinya.
   **Diperbaiki:** file credential & DB internal ditolak semua tool file,
   di-mask di mount sandbox `shell_run`/`git_*`; `vault:` menolak
   credential aplikasi (+ allowlist opsional `OPENCLAWN_HTTP_VAULT_KEYS`);
   trust mode tak bisa melewati `http_request` ber-`vault:`.
   **Residual (jujur):** file workspace lain tetap bisa dibaca lalu dikirim
   via `web_fetch` — sifat bawaan agent ber-tool + web tanpa approval.

### 🟠 Tinggi — bug di inovasi inti & jalur LLM
4. **Inovasi 2: throttle decay tak pernah bekerja + decay berlipat** —
   `_last_decay_ts` per instance (AgentLoop baru tiap request) dan eksponen
   `hari_sejak_dipakai` diterapkan ulang tiap pass. Direproduksi: skill 2
   hari tak dipakai 1.0→0.54 dalam 10 turn (akan terarsip ~20 turn, bukan
   ~40 hari). Plus `last_used_at` waktu lokal vs `julianday('now')` UTC.
   **Diperbaiki:** throttle di `app_settings` (klaim atomik CAS), eksponen
   inkremental sejak pass terakhir, timestamp UTC.
5. **Inovasi 3: evaluator bisa lebih lemah dari generator** lewat fallback
   chain (evaluator gagal → gemma4:e4b, tetap "verified"), dan
   `generator_model` = pilihan router, bukan model yang sebenarnya menjawab.
   **Diperbaiki:** chunk `fallback` saat evaluasi → unverified/draft (refine
   skipped); `Turn.models_used` + `generator_model_of` (multi-model → draft).
6. **API key hilang lolos dari fallback chain** (`ValueError` dari Vault) —
   direproduksi; dengan `refine_on_correction=True` default, pesan koreksi
   bisa menjatuhkan turn berulang. **Diperbaiki:** `ProviderUnavailable`,
   `resolve_previous`/refine dibungkus fail-soft, baris pending tetap resolved.
7. **Tool calling Claude rusak total** — input `{}` (`input_json_delta`
   diabaikan), `input_tokens` hilang, role `tool` ditolak API (400).
   Direproduksi. 8. **Gemini tak pernah melihat hasil tool** (pesan tool
   dibuang; Gemini = tier default COMPLEX/CRITICAL). 9. **Schema tool Ollama
   format Anthropic** (bukan `{"type":"function",...}`). **Diperbaiki:**
   adapter eksplisit per provider (`to_anthropic_messages`,
   `to_gemini_contents`, `to_ollama_*`), ID tool call, semua tool call per
   hop dieksekusi (dulu hanya terakhir), usage dijumlah antar hop, retry
   hanya untuk error transien, event `error` Anthropic → fallback.
10. **`grep`/`glob` mengikuti symlink keluar workspace** — direproduksi.
    **Diperbaiki** + scan dipindah ke thread (tak memblokir event loop).
11. **RBAC tak ditegakkan:** `db_query` membaca seluruh DB lintas user
    (approval diputuskan peminta sendiri); role `viewer` tak dicek di mana
    pun. **Diperbaiki:** `db_query` admin-only saat auth aktif (+ tabel
    `users`/`mcp_servers` selalu ditolak); endpoint mutasi butuh ≥ member.
12. **OIDC tanpa allowlist** — akun apa pun di IdP publik jadi member (akun
    pertama admin). **Diperbaiki:** `OPENCLAWN_OIDC_ALLOWED_EMAILS/_DOMAINS`
    (+ `email_verified` wajib). *Default tetap permisif bila kosong*
    (kompatibilitas mundur) dengan peringatan startup — **owner bisa
    memutuskan menjadikannya wajib.**

### 🟡 Sedang (semua diperbaiki)
- SSRF: DNS rebinding (guard resolve ≠ resolve saat connect) → validasi IP
  di network backend httpcore (`_PublicOnlyBackend`); `getaddrinfo` guard ke
  thread.
- `/chat/stream` tanpa cek pemilik `session_id`; `/converse/stream` bisa
  mengambil alih percakapan aktif user lain → 403.
- Sandbox: proses docker tak di-kill saat timeout; `run_python` crash pada
  output non-UTF8.
- Open redirect `next=/\evil.com` → `_safe_next`.
- `Dockerfile.role` memasang extras `[dev]` → dihapus. `docker-compose.yml`:
  tool sandbox tak tersedia (tanpa Docker CLI/socket) — **didokumentasikan,
  sengaja tidak di-mount docker.sock** (= root host).

### 🟢 Rendah (semua diperbaiki)
- Trigger skill `LIKE '%<60 char task>%'` hampir tak pernah cocok → pencocokan
  substring literal atau ≥60% kata.
- Sinyal koreksi terlalu longgar (`harusnya`, `should be`, `no, `, "salah
  satu") → sinyal lemah hanya di awal pesan, batas kata.
- Crystallizer tanpa `tenant_id`; task `_post_turn` tanpa referensi kuat.
- `RoleNegotiator` tak dipakai di mana pun — **sengaja dibiarkan** (bagian
  Inovasi 4, CLAUDE.md §6 melarang memangkasnya).

Diverifikasi via `uv run`: **1205 passed** (dari 1118; +87 test di 3 file baru
`test_credential_hardening.py`, `test_memory_isolation.py`,
`test_provider_adapters.py` + tambahan di file existing), ruff check/format
bersih, tanpa dependency baru (httpcore sudah dependency transitif httpx).
Plus `tests/conftest.py` baru: memulihkan `infra.config.CONFIG` antar test.

### Putaran 2 (sesi yang sama, setelah commit putaran 1)

Modul yang belum dibaca mendalam di putaran 1 — fokus ke interaksinya dengan
perbaikan di atas. Semua diperbaiki:

- **`late_execute` memakai folder sesi tersimpan tanpa validasi allowlist** —
  celah #1 lolos lewat approval yatim (`session_workspace="/"` + `file_write`
  yang di-approve). Kini divalidasi terhadap allowlist user yang menyetujui.
- **Approval yatim diklaim SETELAH tool dieksekusi** — dua `POST /approve`
  bersamaan menjalankan tool destruktif dua kali (docstring lama menyebutnya
  "risiko fail-soft yang diterima"). Kini klaim atomik dulu, eksekusi hanya
  oleh pemenang. Ditest dengan `asyncio.gather`.
- **`task_graph_submit` terpotong `tool_timeout_sec` (40s) padahal timeout
  node 300s** — direproduksi: baris `task_graphs` macet `running` selamanya,
  subtask tetap jalan tanpa induk, hasil hilang. Kini `Tool.timeout_sec`
  (graph: `task_graph_timeout_sec`=1800) + executor membatalkan anak & menutup
  graph saat dibatalkan.
- **Proposal subtask tanpa owner** → terlihat semua user di `/autopilots`
  lengkap `tool_input`; subtask kini mewarisi `user_id` pemilik graph,
  proposal mencatat owner, halaman memfilter.
- **"Hapus chat" meninggalkan arsip L4** (transkrip penuh, tetap dicari FTS
  & disuntik ke prompt) dan checkpoint L1 sesi → ikut dihapus.
- **`pdf_write`**: teks LLM masuk `reportlab.Paragraph` tanpa escape
  (mini-markup `<img src=...>` bisa menyematkan file lokal di luar workspace);
  kini di-escape. I/O dokumen dipindah ke thread.
- `skill_pack` import URL & MCP remote: DNS guard ke thread; `skill_pack`
  memakai client dengan validasi IP saat connect. **Catatan jujur:** MCP
  remote memakai httpx milik SDK `mcp`, jadi validasi saat connect TIDAK
  berlaku di sana (URL hanya bisa diisi admin).

Diperiksa, bersih: `EventBus` percakapan multi-agent dibuat per orkestrator
(tak ada kebocoran token/approval_id antar percakapan bersamaan).

1213 passed (+8), ruff bersih.

### Putaran 3 — `security/` + sisa tools (2026-09-26)

- 🔴 **CSRF lintas-situs → RCE saat auth nonaktif (default).** Auth OFF =
  CSRF OFF; POST lintas-situs ke `localhost:8000/mcp/add` mendaftarkan server
  MCP stdio (`sh -c ...`) dan langsung menjalankannya di host — direproduksi.
  Juga `/chat/stream` + `trust_mode=true`, `/settings`, `/skills/import`.
  **Diperbaiki:** gerbang origin (`Sec-Fetch-Site`/`Origin` vs `Host`) untuk
  semua method pengubah state, SELALU aktif; plus allowlist Host saat auth
  nonaktif (anti DNS rebinding, `OPENCLAWN_ALLOWED_HOSTS`). *Perubahan
  perilaku:* tanpa auth, akses via IP LAN/domain lain kini 403 kecuali host
  didaftarkan — disengaja (lebih baik aktifkan auth).
- 🟠 **Batas absolut sesi 7 hari tak berlaku saat idle timeout aktif** —
  refresh cookie me-reset satu-satunya timestamp; direproduksi valid >30
  hari. Token kini membawa `iat` (waktu login) terpisah dari `ts`.
- 🟠 **Kunci API Anthropic tak pernah diredaksi PII rail** (regex berhenti di
  tanda hubung `sk-ant-`); ditambah `sk-proj-`, `tvly-`, `github_pat_`,
  Slack. Kartu kredit kini wajib Luhn (dulu timestamp/ID ikut diredaksi).
- 🟡 **Input rail memblokir pesan apa pun yang menyebut "system prompt"**
  (pola Shield terlalu lebar) → pola serangan spesifik.
- 🟡 **Rate limit bisa di-bypass saat auth OFF** dengan cookie sesi acak
  (kunci = cookie mentah) → kunci = IP; key idle disapu (dulu bocor memori).
- 🟡 **`build_sandbox_image` menerima URL/VCS/path langsung** di
  requirements (docstring mengklaim dicegah) → allowlist PEP 508 by-nama.

Diperiksa, bersih: `ApprovalGate` request/resolve/listing (konsisten dengan
gerbang kepemilikan web), `QuestionGate`.

1240 passed (+27), ruff bersih.

### Putaran 4 — keputusan owner-delegated + sisa modul (2026-09-26)

Owner: "lanjutkan dan berikan keputusan terbaik yang sesuai kebutuhan pasar".
Acuan: OpenCLAWN diposisikan self-host/enterprise yang menjual governance →
**secure-by-default tanpa mengunci deployment yang sudah jalan**, dan fitur
inti agent (riset web, eksekusi kode) tetap mulus.

**Keputusan (sebelumnya menunggu owner, lihat putaran 1):**
1. **OIDC tanpa allowlist → pendaftaran akun BARU ditutup** (user lama tetap
   masuk, user pertama tetap bootstrap admin). Opt-in perilaku lama:
   `OPENCLAWN_OIDC_OPEN_SIGNUP=true`. Alasan: pasar enterprise mengharapkan
   secure-by-default; "wajibkan allowlist" mentah akan mengunci user lama saat
   upgrade.
2. **Sandbox di docker-compose via sidecar Docker-in-Docker** (override
   `docker-compose.sandbox.yml`), BUKAN mount `docker.sock` (= root host).
   Eksekusi kode adalah fitur yang diharapkan pasar dari agent framework;
   DinD adalah pola standar industri (CI). Residual risk (privileged dind)
   didokumentasikan jujur + saran gVisor/VM.
3. **`web_fetch` approval berbasis taint** — riset web murni tetap tanpa
   klik (inti UX), tapi setelah turn membaca data privat (file workspace, DB,
   memori, MCP), `web_fetch` butuh approval. Memutus satu kaki "lethal
   trifecta" dengan friksi minimal; trust mode boleh melewati.

**Temuan & perbaikan tambahan:**
- 🟠 **`code_run` gagal di SETIAP panggilan pada host Linux** — skrip di
  `TemporaryDirectory` 0700 tak terbaca user `nobody` di container. Tak
  terlihat di macOS (Docker Desktop melonggarkan izin). Dibuktikan dengan
  simulasi Linux; kini dir 0755/skrip 0644 + `OPENCLAWN_SANDBOX_TMPDIR`.
- 🟠 **Skill tak pernah benar-benar disuntik** — prompt hanya memuat NAMA
  skill, bukan langkahnya; Inovasi 2/3 nyaris tanpa efek. Kini isi
  Trigger/Steps/Outcome (3 teratas, dibatasi token).
- 🟠 **Prompt caching Claude tak pernah kena** — memori dinamis ikut blok
  ber-`cache_control`. Kini soul (stabil) dan konteks dinamis dipisah.
- 🟡 Riwayat Anthropic bisa diawali giliran assistant (400) → placeholder.
- 🟡 Keyword router substring (`plan` di "explanation") → awal kata.
- 🟡 File backup DB mengikuti umask (0644) → 0600.
- Perbaikan build: `PIP_DEFAULT_TIMEOUT`/`PIP_RETRIES` (timeout PyPI saat
  verifikasi end-to-end).

Diperiksa, bersih: `core/autopilot.py`, `core/calibration.py`
(opt-in, throttle DB, langkah ±1), `infra/backup.py` (selain izin file).

**Verifikasi end-to-end sungguhan (bukan cuma unit test)** stack
`docker-compose.yml` + `docker-compose.sandbox.yml` di Docker 29 lokal:
app healthy; dari dalam container app sebagai `appuser` — `run_python`
(Python 3.12 + pandas) sukses, `run_shell` membaca workspace, `.env` di-mask
(0 byte), network terblokir, `/work` read-only, user `nobody`; PID 1 memakai
`DOCKER_HOST=tcp://dind:2376` + sertifikat salinan entrypoint. Uji ini
menemukan 2 bug yang langsung diperbaiki: SAN sertifikat TLS dind tak memuat
nama service (`hostname: dind`), dan timeout PyPI saat build. Catatan jujur:
image sandbox di dalam DinD dimuat dari host (`docker save | docker load`)
karena jaringan sangat lambat (~23 kB/s) — langkah `sandbox-image` sendiri
terbukti terhubung ke dind via TLS dan mulai build, tapi tak ditunggu selesai.
Stack, volume, dan image uji dibongkar setelahnya.

1255 passed (+15), ruff bersih.

### Putaran 5 — sisa modul yang belum dibaca ulang (2026-09-26)

- 🔴 **`/conversations` menampilkan transkrip LENGKAP percakapan multi-agent
  SEMUA user** ke siapa pun yang login; `/activity` menampilkan prompt
  percakapan & detail blocker semua user; `/blockers/resolve` menerima blocker
  id apa pun. Kelas sama dengan kebocoran memori L1 (putaran 1). Kini
  `conversations.owner_user_id` dicatat dan ketiganya difilter per pemilik
  untuk non-admin (transkrip lama tanpa owner: fail-closed untuk non-admin).
- 🟠 **Admin terakhir bisa diturunkan** → deployment OIDC-only terkunci
  permanen. Guard atomik di `UserStore.set_access_role`.
- 🟡 `audit_chain`: komentar mengklaim append-only DITEGAKKAN trigger, padahal
  hanya DELETE — trigger BEFORE UPDATE ditambah; `verify()` per batch (dulu
  memuat seluruh rantai sekaligus, membekukan event loop).
- 🟡 `apply_manifest`: newline di nilai menghasilkan soul.toml invalid (role
  tak bisa dimuat), key/nama tool bisa menyuntik section TOML, role tak
  divalidasi, tulis non-atomik → escape JSON, identifier aman, parse-check
  sebelum tulis, `os.replace`.
- 🟡 Jaring token `_truncate_tool_output` hanya memotong string — dict/list
  besar lolos ke context → ikut dipotong.
- 🟢 `/metrics/prometheus`: token bearer opsional `OPENCLAWN_METRICS_TOKEN`.

Diperiksa, bersih: `core/sandbox_reaper.py`, `tools/sandbox_persist.py`,
`tools/data.py::JsonQueryTool`, `/metrics` (hanya agregat),
`core/prometheus_metrics.py` (agregat, tanpa PII).

1266 passed (+11), ruff bersih.

### Putaran 6 — memory/ & infra/logging (2026-09-26)

- 🟠 **Curator (I1) menulis ulang skill memakai model TERLEMAH** — judge
  di-hardcode `gemma4:e4b` padahal menghasilkan `merged_content` pengganti isi
  skill dari gemini-2.5-pro/Claude (melanggar aturan inti evaluator ≥
  generator). Kini `EVALUATOR_FOR` per generator; beda/tak dikenal atau
  fallback → tak di-merge.
- 🟡 **Skrip CLI berjalan tanpa scrubber log** — `setup_logging()` hanya
  dipanggil lifespan web; kini dipasang saat modul diimpor. Hint `token`
  substring me-redact `tokens_in`/`max_tokens` → segmen kata. Pola `tvly-`/
  `github_pat_` ditambah.
- 🟡 **UserModel (I5) per-role bocor lintas user bila diaktifkan di mode
  multi-user** → dinonaktifkan saat auth aktif. **Catatan produk:** I5
  praktis dorman — tak ada kode produksi yang menulis `memory_l2`.

Diperiksa, bersih: `tools/todo.py`, `tools/blocker.py`, `GET /tasks/{id}`
(kepemilikan dicek).

Catatan proses: pemeriksa izin otomatis Bash beberapa kali timeout di sesi
ini; pekerjaan dilanjutkan lewat tool Edit/Read, tak ada langkah terlewati.

1273 passed (+7), ruff bersih.

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
