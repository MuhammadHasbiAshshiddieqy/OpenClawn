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

**Constraint:** `UNIQUE(role, skill_name)` — nama skill unik per role.  
**Index:** `idx_skills_active` pada `(role, status, decay_score DESC)` — untuk query `get_active_skills`.

Status lifecycle: `active` → (decay) → `archived` → (mark_used) → `active` lagi.

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

Override dianggap aktif hanya jika **kedua** key terisi. Menghapus salah satu (set kosong) mengembalikan ke mode router otomatis.

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
