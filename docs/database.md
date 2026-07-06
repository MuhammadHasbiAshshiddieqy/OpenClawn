# Database Schema — `migrations/001_initial.sql`

OpenCLAWN menggunakan SQLite (aiosqlite) dengan WAL mode. Satu file `data/openclawn.db`. Semua tabel dibuat via `migrations/001_initial.sql`.

---

## Tabel Memory

### `memory_l1` — Key-Value Checkpoint (L1)

State terbaru agent per role. Di-update tiap turn.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `role` | TEXT | Role agent (`pm`, `qa`, `dev`) |
| `key` | TEXT | Key (saat ini: `"last_summary"`) |
| `value` | TEXT | Nilai (maks 500 karakter) |
| `updated_at` | TIMESTAMP | Waktu update terakhir |

**Constraint:** `UNIQUE(role, key)` — satu row per kombinasi role+key. UPSERT via `ON CONFLICT DO UPDATE`.

---

### `memory_l2` — Facts (L2)

Fakta semi-permanen yang diketahui agent per role.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | |
| `role` | TEXT | Role yang punya fakta ini |
| `fact` | TEXT | Isi fakta |
| `importance` | INTEGER | Prioritas load (default 1) |
| `locale` | TEXT | Locale fakta (default `"neutral"`) |
| `created_at` | TIMESTAMP | |

**Index:** `idx_l2_role` pada `(role, importance DESC)` — untuk load yang cepat.

---

### `session_turns` — Transkrip Percakapan Per-Sesi

Riwayat giliran (user/assistant) untuk single-agent chat, ber-`session_id`. `AgentLoop` dibuat baru tiap request web (`self.history` kosong di awal), jadi tanpa tabel ini turn N+1 tak melihat turn N walau di sesi sama (§ user report). Ditulis di finalize (`MemoryManager.append_turn`), dimuat kembali di awal turn (`load_turns`). Dipisah dari `conversations` (multi-agent, per-transkrip) karena granularitasnya per-giliran.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | Urutan giliran (ASC = lama→baru) |
| `session_id` | TEXT | Sesi pemilik giliran |
| `role` | TEXT | `"user"` atau `"assistant"` |
| `content` | TEXT | Isi giliran (versi teredaksi bila guardrail OUTPUT aktif) |
| `created_at` | TIMESTAMP | |

**Index:** `idx_session_turns` pada `(session_id, id)` — load per-sesi urut.

---

### `session_workspace` — Folder Kerja Aktif Per-Sesi

Folder kerja aktif untuk satu sesi chat, bisa diubah agent sendiri lewat tool `set_workdir` (§ user request: "pindah direktori secara dinamis" lewat chat — sebelumnya folder kerja HANYA bisa diubah lewat field UI sekali per-request). Satu baris per sesi (UPSERT) — beda dari `session_turns` yang append-log, ini state "saat ini", bukan riwayat. Dibaca `AgentLoop.run()` di awal turn (fallback bila form `workdir` kosong), ditulis `SetWorkdirTool` saat sukses.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `session_id` | TEXT PK | Sesi pemilik folder aktif |
| `workdir` | TEXT | Path absolut folder kerja aktif |
| `updated_at` | TIMESTAMP | |

---

### `chat_sessions` — Metadata Sidebar Riwayat Chat

Metadata TAMPILAN (judul, waktu, role) untuk sidebar riwayat chat single-agent (§ user report: "chat selalu ke-reset", tak ada cara buka chat baru/lanjutkan/hapus riwayat). Terpisah dari `session_turns` (transkrip per-giliran) — tabel ini murni untuk daftar di sidebar, bukan isi percakapan.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `session_id` | TEXT PK | Sesi yang direpresentasikan |
| `role` | TEXT | Role aktif saat sesi dibuat (label kecil di item sidebar) |
| `title` | TEXT | `NULL` sampai turn pertama selesai; di-generate LLM lokal dari potongan pesan pertama (lihat `infra/chat_sessions.py`) |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | Diperbarui tiap turn (`ChatSessionStore.touch`) — dipakai urutan sidebar (terbaru dulu) & grouping bucket waktu |
| `deleted_at` | TIMESTAMP | `NULL` = aktif. Diisi saat user hapus riwayat (soft-delete — metadata tetap ada untuk audit trail lama), TAPI `session_turns`/`session_workspace` terkait dihapus FISIK saat itu juga |

**Index:** `idx_chat_sessions_active` pada `(deleted_at, updated_at DESC)` — load daftar sidebar cepat (filter aktif + urut terbaru).

---

### `skills` — Skill Store (L3 + Decay)

Skill yang dipelajari agent beserta metadata decay.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | |
| `role` | TEXT | Role pemilik skill |
| `skill_name` | TEXT | Nama unik skill (slug dari task) |
| `trigger_pattern` | TEXT | Pola query yang memicu skill ini |
| `skill_content` | TEXT | Konten skill dalam Markdown |
| `visibility` | TEXT | `private` / `shared` / `inherited` |
| `status` | TEXT | `active` / `draft` / `archived` |
| `confidence` | REAL | Skor confidence dari crystallizer (0–1) |
| `generator_model` | TEXT | Model yang menghasilkan skill (untuk evaluator gating) |
| `use_count` | INTEGER | Berapa kali skill dipakai |
| `last_used_at` | TIMESTAMP | Waktu terakhir dipakai |
| `decay_score` | REAL | Skor decay saat ini (1.0 = fresh, 0.0 = habis) |
| `created_at` | TIMESTAMP | |
| `merged_into` | INTEGER | I1: bila skill ini diserap merge → id winner (status `merged`) |
| `version` | INTEGER | I3: dinaikkan tiap refine/merge (riwayat di `skill_versions`) |
| `draft_success_count` | INTEGER | I2: berapa kali draft dipakai-sukses (→ promote di ambang `draft_promote_uses`) |

**Constraint:** `UNIQUE(role, skill_name)` — nama skill unik per role.  
**Index:** `idx_skills_active` pada `(role, status, decay_score DESC)` — untuk query `get_active_skills`.

Status lifecycle: `active` → (decay) → `archived` → (mark_used) → `active` lagi; `draft` → (I2 promote) → `active`; `active` → (I1 merge) → `merged` (revertible). Status `merged` = isi diserap skill lain, tidak dihapus.

---

## Tabel Session Archive

### `memory_l4` — FTS5 Cross-Session Archive (L4)

Virtual table FTS5 untuk full-text search lintas sesi.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `role` | TEXT | Role sesi |
| `session_id` | TEXT | ID sesi yang diarsipkan |
| `summary` | TEXT | Ringkasan sesi (maks ~200 karakter) |
| `full_content` | TEXT | Transkrip lengkap sesi |
| `created_at` | TEXT | UNINDEXED — tidak masuk FTS index |

**Catatan:** FTS5 tidak mendukung `UNIQUE` constraint. Idempotency dijaga lewat DELETE-then-INSERT di `MemoryManager.archive_session()`.

Query FTS5:
```sql
SELECT summary FROM memory_l4
WHERE role=? AND memory_l4 MATCH ? ORDER BY rank LIMIT 3
```

---

## Tabel Routing Audit

### `routing_events` — Audit Routing (Inovasi 1)

Setiap keputusan routing dicatat sebelum LLM call dan diupdate setelah selesai.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | Dipakai sebagai `event_id` |
| `session_id` | TEXT | ID sesi |
| `role` | TEXT | Role aktif |
| `query_text` | TEXT | Pesan user (untuk debugging) |
| `dim_query_tokens` | INTEGER | Dimensi 1: estimasi token query |
| `dim_has_tech_kw` | INTEGER | Dimensi 2: ada kata teknis? |
| `dim_needs_multistep` | INTEGER | Dimensi 3: butuh multi-langkah? |
| `dim_history_len` | INTEGER | Dimensi 4: panjang history |
| `dim_role` | TEXT | Dimensi 5: role (string) |
| `dim_has_urgency` | INTEGER | Dimensi 6: ada kata urgency? |
| `dim_needs_stream` | INTEGER | Dimensi 7: butuh stream? |
| `dim_is_continuation` | INTEGER | Dimensi 8: lanjutan percakapan? |
| `dim_soul_upgrade_hit` | INTEGER | Apakah soul upgrade_keyword cocok |
| `complexity_score` | INTEGER | Skor numerik final |
| `complexity_label` | TEXT | Label: trivial/simple/moderate/complex/critical |
| `model_chosen` | TEXT | Model yang dipilih |
| `provider` | TEXT | Provider: ollama/anthropic |
| `routing_reason` | TEXT | Penjelasan teks keputusan |
| `fallback_used` | INTEGER | 1 jika fallback chain aktif |
| `tokens_in` | INTEGER | Token input aktual (diupdate setelah turn) |
| `tokens_out` | INTEGER | Token output aktual |
| `cost_usd` | REAL | Estimasi biaya USD |
| `latency_ms` | INTEGER | Latensi total (ms) |
| `had_correction` | INTEGER | 1 jika turn berikutnya mengoreksi ini |
| `correction_detail` | TEXT | Pesan koreksi user |
| `evidence_json` | TEXT | Evidence-Based Response (§ Prioritas 2): snapshot JSON `{policy, memory, guardrail}` yang berlaku turn ini, diisi `RoutingAuditor.finalize(evidence=...)`. `NULL` bila turn belum selesai atau dari versi lama tanpa evidence. Query-able via `GET /evidence/{id}` |
| `created_at` | TIMESTAMP | |

**Index:** `idx_routing_label` pada `(complexity_label, had_correction)` — untuk `calibration_report`.

---

## Tabel Role Handoffs

### `role_handoffs` — Handoff Log (Inovasi 4)

Semua handoff antar role dicatat, valid maupun tidak.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | |
| `session_id` | TEXT | Sesi yang memicu handoff |
| `from_role` | TEXT | Role pengirim |
| `to_role` | TEXT | Role penerima |
| `task_input` | TEXT | Task yang diberikan |
| `contract_name` | TEXT | Nama contract yang dipakai |
| `output_json` | TEXT | Output dalam JSON (bisa raw jika validasi gagal) |
| `validation_ok` | INTEGER | 1 jika validasi Pydantic berhasil, 0 jika tidak |
| `created_at` | TIMESTAMP | |

---

## Tabel Approval Log

### `approval_log` — HITL Approval Log

Semua permintaan approval tool destruktif.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | |
| `session_id` | TEXT | Sesi yang meminta |
| `tool_name` | TEXT | Tool yang diminta |
| `tool_input` | TEXT | Input dalam JSON |
| `decision` | TEXT | `approved` / `rejected` / `timeout` / `pending:{id}` |
| `created_at` | TIMESTAMP | |

Saat `request()` dipanggil: diinsert dengan `decision="pending:{approval_id}"`. Setelah user memutuskan: diupdate ke `"approved"`, `"rejected"`, atau `"timeout"`.

---

## Tabel App Settings

### `app_settings` — Override Runtime

Key-value sederhana untuk setting yang bisa diubah lewat `/settings` tanpa restart. Dikelola oleh `SettingsStore` ([infra.md](infra.md)).

| Kolom | Tipe | Keterangan |
|---|---|---|
| `key` | TEXT PK | Nama setting |
| `value` | TEXT | Nilai (string) |
| `updated_at` | TIMESTAMP | Waktu update terakhir |

Key yang dipakai saat ini:
- `model_override_provider` — provider override (`ollama`/`anthropic`/`gemini`)
- `model_override_model` — nama model override
- `router_threshold_offset` — offset threshold kalibrasi (int), dibaca `SmartRouter` tiap turn
- `router_model_map` — JSON override peta tier→model (`RouterConfigStore`), dibaca tiap turn → `router.model_map`
- `guardrails_enabled` — JSON on/off per rail guardrail (`GuardrailConfigStore`), dibaca tiap turn → `GuardrailEngine`; tanpa key = semua rail aktif

Override model dianggap aktif hanya jika **kedua** key model terisi. Menghapus salah satu (set kosong) mengembalikan ke mode router otomatis.

---

### `calibration_log` — Jejak Kalibrasi Router (Inovasi 1, loop tertutup)

Audit setiap kali offset threshold router digeser dari rekomendasi kalibrasi. Dikelola `CalibrationStore` ([core.md](core.md)). Menutup loop: audit → rekomendasi → **apply** (tercatat di sini) → revert.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `old_offset` | INTEGER | Offset sebelum apply (juga target saat revert) |
| `new_offset` | INTEGER | Offset sesudah apply |
| `reason` | TEXT | Ringkasan rekomendasi pemicu (mis. `simple/under_provisioned`) |
| `source` | TEXT | `calibration` \| `revert` \| `manual` |
| `active` | INTEGER | `1` = state aktif terakhir; `0` = sudah digantikan/di-revert |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_calibration_active` pada `(active)` — cari baris aktif cepat. Invarian: tepat satu baris `active=1` setelah apply/revert pertama.

---

### `tool_invocations` — Telemetri Penggunaan Tool

Audit setiap eksekusi tool, dicatat terpusat di `AgentLoop._execute_tool` lewat `ToolAudit` ([core.md](core.md)). Menjawab "tool mana berguna / sering gagal". Ditampilkan di `/metrics`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `session_id` | TEXT | Sesi yang memanggil |
| `role` | TEXT | Role agent |
| `tool_name` | TEXT | Nama tool |
| `outcome` | TEXT | `ok` \| `error` \| `timeout` |
| `latency_ms` | INTEGER | Durasi eksekusi |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_tool_invocations` pada `(tool_name, outcome)` — agregasi per tool cepat. Penulisan fail-soft (error tulis hanya di-log, tak menjatuhkan turn).

---

### `crystallization_log` — Jejak Kristalisasi (Inovasi 3 observability)

Setiap percobaan kristalisasi (termasuk yang jadi `draft`/`duplicate`) dicatat oleh `ConfidenceCrystallizer._log_attempt`. Tabel `skills` hanya menyimpan hasil tersimpan; ini membuat **keputusan evaluator** kasat mata di `/skills`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `role` | TEXT | Role agent |
| `skill_name` | TEXT | Nama skill (slug task) |
| `generator_model` | TEXT | Model yang menghasilkan solusi |
| `evaluator_model` | TEXT | Model evaluator (minimal setara generator) |
| `confidence` | INTEGER | 1..5 dari self-evaluation |
| `critical_gaps` | INTEGER | 1 = ada gap kritis |
| `status` | TEXT | `active` \| `draft` \| `duplicate` |
| `reasoning` | TEXT | Satu kalimat alasan evaluator |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_crystallization_role` pada `(role, status)`. Penulisan fail-soft.

---

### `conversations` — Arsip Percakapan Multi-Agent

Transkrip percakapan multi-agent (pipeline/debate/orchestrator) disimpan oleh `ConversationOrchestrator._persist` di setiap `conversation_end`, agar bisa ditinjau ulang di `/conversations` (sebelumnya ephemeral).

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `session_id` | TEXT | Sesi percakapan |
| `pattern` | TEXT | `pipeline` \| `debate` \| `orchestrator` |
| `participants` | TEXT | CSV peserta (lead-first untuk orchestrator) |
| `initial_message` | TEXT | Pesan pembuka |
| `transcript_json` | TEXT | JSON `[[role, content], ...]` |
| `turns` | INTEGER | Jumlah giliran agent |
| `end_reason` | TEXT | `strategy_done` \| `max_turns` \| `stopped` |
| `cost_usd` | REAL | Total biaya lintas-giliran |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_conversations_session` pada `(session_id)`. Satu baris per run (persist hanya di `conversation_end`). Penulisan fail-soft.

---

### `agent_todos` — Rencana Langkah Agent (tool `todo_write`)

Daftar langkah multi-step yang dikelola agent lewat tool `todo_write`, per sesi. Tiap panggilan **mengganti** seluruh daftar sesi (snapshot). Membuat rencana kerja agent terlihat user.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `session_id` | TEXT | Sesi pemilik daftar |
| `position` | INTEGER | Urutan item dalam daftar |
| `content` | TEXT | Isi langkah |
| `status` | TEXT | `pending` \| `in_progress` \| `completed` |
| `updated_at` | TIMESTAMP | — |

**Index:** `idx_agent_todos_session` pada `(session_id, position)`. `session_id` disuntik AgentLoop sebagai `_session_id` (model tak mengarang sesi).

---

### `agent_blockers` — Hambatan Terstruktur (tool `report_blocker`)

Hambatan yang dilaporkan agent secara terstruktur (terinspirasi *proactive blocker reporting* Multica). Beda dari `ask_user` (yang MEMBLOKIR menunggu jawaban): blocker **asinkron** — agent melaporkan lalu lanjut/berhenti, user meninjau & menutup kapan saja di `/activity`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `session_id` | TEXT | Sesi pelapor |
| `role` | TEXT | Role agent (disuntik `_role`) |
| `summary` | TEXT | Ringkas: apa yang menghambat |
| `detail` | TEXT | Konteks tambahan (opsional) |
| `severity` | TEXT | `low` \| `medium` \| `high` |
| `status` | TEXT | `open` \| `resolved` |
| `created_at` | TIMESTAMP | — |
| `resolved_at` | TIMESTAMP | Saat user menutup (NULL = masih terbuka) |

**Index:** `idx_agent_blockers_status` pada `(status, created_at DESC)`.

---

### `autopilots` — Jadwal Tugas Agent Terjadwal

Definisi tugas berulang yang dijalankan otomatis (terinspirasi *Autopilots* Multica). Dijalankan `AutopilotScheduler` (loop asyncio in-process). **Keamanan (§1, §17):** autopilot berjalan dengan `autopilot=True` → tool butuh-approval TIDAK dieksekusi, diantri sebagai proposal.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `name` | TEXT | Nama jadwal |
| `role` | TEXT | Role yang menjalankan tugas |
| `prompt` | TEXT | Instruksi tugas terjadwal |
| `interval_sec` | INTEGER | Jeda antar-jalan (detik, UTC, tanpa cron); min 60 |
| `enabled` | INTEGER | 1 = aktif, 0 = jeda |
| `last_run_at` | TIMESTAMP | Terakhir dijalankan (NULL = belum) |
| `next_run_at` | TIMESTAMP | Due berikutnya (dihitung scheduler, misfire-safe) |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_autopilots_due` pada `(enabled, next_run_at)` — cari yang due cepat.

---

### `autopilot_runs` — Riwayat Eksekusi Autopilot

Satu baris per run autopilot, untuk ditinjau di `/autopilots`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `autopilot_id` | INTEGER | Autopilot yang dijalankan |
| `session_id` | TEXT | Sesi run (`autopilot-{id}`) — tautkan ke routing_events dll |
| `status` | TEXT | `running` \| `done` \| `error` |
| `output` | TEXT | Ringkasan jawaban agent |
| `proposals` | INTEGER | Jumlah aksi destruktif yang DIANTRI (bukan dieksekusi) |
| `error` | TEXT | Pesan error bila gagal |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_autopilot_runs` pada `(autopilot_id, id DESC)`.

**Catatan `approval_log`:** autopilot mengantri aksi destruktif dengan `decision='proposal:pending'` (bukan `pending:{id}` seperti approval interaktif). Ditampilkan di `/autopilots` sebagai proposal menunggu tinjauan.

---

### `curation_log` — Jejak Konsolidasi Skill (I1)

Setiap usulan/merge/revert skill mirip dicatat oleh `SkillCuratorManager`. Loser tidak dihapus (revertible); tabel ini membuat keputusan merge kasat mata di `/skills`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `role` | TEXT | Role pemilik skill |
| `action` | TEXT | `merge` \| `revert_merge` |
| `status` | TEXT | `pending` \| `applied` \| `reverted` (default `applied`) — lihat catatan gating di bawah |
| `winner_id` | INTEGER | Skill yang bertahan (hasil sintesis) |
| `loser_ids` | TEXT | JSON array id skill yang diserap (status `merged` setelah diterapkan) |
| `similarity` | REAL | Skor pre-filter leksikal Jaccard (0..1) |
| `judge_confidence` | INTEGER | 1..5 dari LLM judge |
| `merged_content` | TEXT | Konten sintesis judge; disimpan di baris `pending` sampai di-apply |
| `reasoning` | TEXT | Satu kalimat alasan judge |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_curation_role` pada `(role, created_at DESC)`. Merge diusulkan hanya bila judge ≥ `curation_judge_min_confidence`.

**Gating `curation_auto` (§8, default `False`):** judge yang menyetujui merge TIDAK langsung mengubah skill — baris `curation_log` ditulis dengan `status='pending'` dan `skills` tetap `active`, menunggu tombol **Terapkan** manusia di `/skills` (`POST /skills/apply-merge` → `SkillCuratorManager.apply_pending_merge`). Bila `curation_auto=True`, merge diterapkan langsung dengan `status='applied'`. `revert_last_merge` hanya melihat baris `status='applied'` — usulan `pending` belum mengubah apa pun sehingga tidak perlu (dan tidak bisa) di-revert.

---

### `skill_versions` — Riwayat Versi Skill (I3/I1 — revertible)

Konten skill SEBELUM tiap refine/merge, agar perubahan dapat ditinjau & dibatalkan.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `skill_id` | INTEGER | Skill yang berubah |
| `version` | INTEGER | Versi konten yang disimpan (sebelum perubahan) |
| `skill_content` | TEXT | Konten lama |
| `reason` | TEXT | `refine_on_correction` \| `merge` |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_skill_versions_skill` pada `(skill_id, version DESC)`.

---

### `skill_usage_pending` — Jembatan Outcome Antar-Turn (I2/I3)

AgentLoop dibuat baru tiap request → "skill apa dipakai turn lalu" harus dipersistenkan agar turn berikutnya (yang membawa `had_correction`) bisa menentukan outcome: sukses → revive/promote; dikoreksi → reset/refine. Dikelola `SkillFeedback`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `session_id` | TEXT | Sesi pemilik |
| `role` | TEXT | Role agent |
| `skill_ids` | TEXT | JSON array skill_id yang dipakai turn itu |
| `resolved` | INTEGER | 1 = outcome sudah diproses |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_skill_usage_pending` pada `(session_id, resolved, id DESC)`.

---

### `user_model` — Profil User Naratif (I5, opsional)

Ringkasan naratif tentang user (dari L2 facts), disuntik sebagai blok stabil di context. Aktif hanya bila `user_model_enabled`. Versioned + revertible; dapat dihapus user (privasi §1). Dikelola `UserModel`.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `role` | TEXT | Role pemilik lensa |
| `version` | INTEGER | Versi profil |
| `profile` | TEXT | Ringkasan naratif (maks ~600 char) |
| `active` | INTEGER | 1 = versi aktif yang disuntik |
| `created_at` | TIMESTAMP | — |

**Index:** `idx_user_model_role` pada `(role, active, version DESC)`.

---

### `mcp_servers` — Server MCP Eksternal

Definisi server Model Context Protocol yang disambungkan agar agent memakai tool dari ekosistem MCP. Dikelola `MCPRegistry`; tool yang ditemukan dibungkus `MCPTool` (selalu butuh approval, §1) & didaftarkan ke `TOOL_REGISTRY` saat startup.

| Kolom | Tipe | Keterangan |
|---|---|---|
| `id` | INTEGER PK | — |
| `name` | TEXT UNIQUE | Nama unik (dipakai di prefix `mcp__<name>__tool`) |
| `transport` | TEXT | `stdio` (subprocess lokal) \| `http` (remote, SSRF-guarded) |
| `command` | TEXT | stdio: argv sebagai JSON array |
| `url` | TEXT | http: endpoint server MCP |
| `env` | TEXT | stdio: env tambahan sebagai JSON object |
| `enabled` | INTEGER | 1 = dimuat saat startup |
| `created_at` | TIMESTAMP | — |

Tool MCP TIDAK mendapat jalur istimewa: lewat pagar yang sama (izin per-role via `mcp__*` di soul.toml, validasi schema, telemetri, approval).

**Catatan `app_settings`:** key baru `calibration_auto_last_ts` (throttle I4), `curation_last_ts:{role}` & `user_model_last_ts:{role}` (throttle I1/I5), serta `calibration_log.source='auto'` (I4 auto-apply).

---

## Query Penting

```sql
-- Berapa banyak skill aktif per role?
SELECT role, COUNT(*) FROM skills WHERE status='active' GROUP BY role;

-- History routing per sesi
SELECT complexity_label, model_chosen, latency_ms, cost_usd
FROM routing_events WHERE session_id='...' ORDER BY id;

-- Correction rate per label (= output calibration_report)
SELECT complexity_label,
       COUNT(*) as total,
       SUM(had_correction) as corrections,
       ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate
FROM routing_events
GROUP BY complexity_label
ORDER BY correction_rate DESC;

-- Skill yang mendekati threshold archive
SELECT skill_name, decay_score, last_used_at
FROM skills WHERE status='active' ORDER BY decay_score ASC LIMIT 10;

-- Approval yang pending (semua sesi)
SELECT * FROM approval_log WHERE decision LIKE 'pending:%';

-- Override model aktif (jika ada)
SELECT key, value FROM app_settings WHERE key LIKE 'model_override_%';
```
