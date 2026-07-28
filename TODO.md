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
2026-07-27 (sesi lanjutan, sama hari): 2 blocker + 3 should-fix **sudah
diperbaiki dan terverifikasi hijau** (801 test lulus, `ruff check`/`ruff
format --check` bersih via Docker `python:3.12-slim` + `uv sync --frozen`,
lihat §"Verifikasi CI sungguhan" di bawah), 1 nice-to-have masih terbuka.

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
  **Sebagian dimitigasi** tanpa dependency baru (enkripsi-at-rest sungguhan
  butuh keputusan dependency kripto → §8, belum diambil): `list_servers()`
  sekarang TIDAK PERNAH mengembalikan nilai `env` mentah ke caller (UI/API) —
  hanya `has_env` (boolean). Mencegah kebocoran lewat read-path publik kalau
  form `/mcp/add` suatu saat menambahkan field env. Kolom DB itu sendiri
  masih plaintext (write-path) — encryption-at-rest sungguhan tetap item
  terbuka, lihat §4 poin 6.
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
- [ ] `infra/logging.py` — pattern-matching secret-scrubber solid sebagai
  defense-in-depth tapi tidak exhaustive; token format tak dikenal di field
  bernama netral tetap lolos ter-log.

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
5. **[belum, perlu keputusan owner]** Encryption-at-rest sungguhan untuk
   `mcp_servers.env` (bukan cuma masking read-path yang sudah dikerjakan) —
   butuh dependency kripto baru (mis. `cryptography`), CLAUDE.md §8 eksplisit
   minta persetujuan owner sebelum menambah dependency. Jangan dikerjakan
   sepihak.
6. ~~Konfirmasi CI hijau di environment 3.12 asli~~ — **selesai**, lihat
   §"Verifikasi CI sungguhan" di §2 (801 passed via Docker `python:3.12-slim`
   + `uv sync --frozen`).
7. **[eksplorasi tren]** Evaluasi kebutuhan nyata A2A/ACP interop — HANYA
   jika ada kebutuhan pilot konkret untuk agent OpenCLAWN dipanggil dari
   atau memanggil agent di platform lain. Jangan bangun spekulatif.
8. **[eksplorasi tren]** Evaluasi credential rotation / scoping JIT untuk
   API key yang dipegang agent (MCP eksternal) — relevan kalau target
   deployment bergeser ke multi-tenant SaaS sungguhan, bukan self-host
   single-org.

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
