# `memory/` — Sistem Memori L1–L4

OpenCLAWN menggunakan sistem memori berlapis. Setiap layer punya kecepatan dan jangkauan berbeda.

```
L1 — Key-value checkpoint   (cepat, per-role, lintas sesi)
L2 — Facts/fakta penting    (semi-permanen, per-role)
L3 — Active skills          (injeksi konteks, dari tabel skills)
L4 — FTS5 session archive   (lintas sesi, full-text search)
```

---

## `memory/layers.py`

Antarmuka utama memori. Semua read/write memori dilakukan lewat kelas ini.

### Konstanta: `SPECIFIC_TERMS`

Kata teknis yang memicu FTS5 search walaupun query pendek:
`bug`, `error`, `oauth`, `api`, `deploy`, `fix`, `crash`

### Kelas: `MemoryManager`

**`__init__(role, session_id, db)`**  
Buat instance terikat pada satu role dan satu sesi.

**`load_context(query, skills) → dict`** *(async)*  
Load semua layer dan kembalikan sebagai dict:

```python
{
    "l1": {"last_summary": "..."},          # key-value dari memory_l1
    "l2": ["fakta 1", "fakta 2", ...],      # list fakta dari memory_l2
    "l3": skills,                            # di-pass langsung (dari SkillDecayManager)
    "l4": ["ringkasan sesi lama", ...],     # dari FTS5 memory_l4 (conditional)
}
```

L4 hanya di-query jika query > 3 kata **atau** mengandung term teknis spesifik. Ini hemat query SQLite untuk percakapan pendek/casual. Query disanitasi lewat `fts5_query()` sebelum MATCH — query bertanda baca (titik, titik dua, kurung) tidak lagi memicu syntax error.

**`load_turns(limit=20) → list[dict]`** *(async)*  
Muat transkrip giliran (`user`/`assistant`) untuk **sesi ini** dari `session_turns`, urut lama→baru, dibatasi `limit` giliran TERBARU. Dipakai `AgentLoop._run()` di awal turn untuk mengisi `self.history` — karena `AgentLoop` dibuat baru tiap request web, tanpa ini turn N+1 tak pernah melihat turn N (§ user report: agent seolah tak baca chat sebelumnya, bahkan di sesi yang sama). Berbeda dari `load_context` (role-scoped, ringkasan): ini per-`session_id`, transkrip penuh per-giliran.

**`append_turn(role, content) → None`** *(async)*  
Simpan satu giliran ke `session_turns` (persist multi-turn). Konten kosong dilewati. Dipanggil `AgentLoop._run()` di finalize (setelah guardrail OUTPUT → transkrip = versi teredaksi) untuk `user` lalu `assistant`. Hanya untuk single-agent (`AgentConfig.persist_history=True`); multi-agent mengelola transkrip sendiri di `turn_input`.

**`update_checkpoint(summary: str) → None`** *(async)*  
Tulis/update L1 key `"last_summary"` dengan konten terbaru (maks 500 karakter). Operasi UPSERT — tidak duplikasi. Catatan: role-scoped (bukan per-sesi) & hanya ringkasan jawaban terakhir; riwayat percakapan sebenarnya kini di `session_turns` (lihat `load_turns`).

Dipanggil tiap turn dari `agent_loop._post_turn()` jika turn punya konten.

**`add_fact(fact, importance=1, locale="neutral") → None`** *(async)*  
Tambah fakta baru ke L2. `importance` digunakan untuk urutan saat di-load (DESC).

**`archive_session(summary, full_content) → None`** *(async)*  
Arsipkan sesi ke L4 (FTS5). **Idempoten per sesi**: hapus arsip lama sesi ini dulu sebelum insert — mencegah duplikat saat dipanggil berulang. Dipanggil dari `_post_turn` setelah `archive_after_turns` tercapai.

**`_has_specific_term(query) → bool`** *(private)*  
Return True jika query mengandung salah satu kata dalam `SPECIFIC_TERMS`.

---

## `memory/skill_decay.py` — Inovasi 2

Skill yang jarang dipakai memudar secara eksponensial dan akhirnya diarsipkan.

### Formula Decay

```
new_score = current_score × (0.97 ^ hari_sejak_dipakai)
```

Skill dengan `decay_score < 0.3` → diarsipkan (`status='archived'`). Skill yang dipakai lagi → revive otomatis.

### Kelas: `SkillDecayManager`

**`__init__(role, db, config, tenant_id="default")`**  
Cache `_last_decay_ts` untuk throttle. Multi-Tenant (TODO.md § Prioritas 5, WIRED PENUH): `tenant_id` opsional, default `'default'` untuk kompatibilitas mundur — semua method di kelas ini men-scope query ke tenant ini, skill milik tenant lain tak pernah terlihat/tersentuh.

**`get_active_skills(query) → list[dict]`** *(async)*  
Ambil skill aktif MILIK TENANT INI yang relevan dengan query (trigger_pattern match). Urutkan berdasarkan `decay_score DESC, use_count DESC`. Maks `config.max_active_skills` skill. **I2:** ditambah 1 slot percobaan untuk skill `draft` yang trigger-nya cocok (agar draft bisa membuktikan diri & naik kelas) — draft trial TIDAK menggusur active.

**Skill Marketplace lintas-role (TODO.md § Prioritas 6):** ditambah bagian ketiga — skill milik role LAIN (dalam tenant yang sama) dengan `visibility IN ('shared','inherited')` dan `status='active'`, di-LIMIT `config.max_shared_skills` (default 3, lebih kecil dari `max_active_skills` — token-first §1.4, skill role sendiri selalu lebih relevan). Ditambahkan di BELAKANG hasil `active`+`trial`, tak menggusurnya. `visibility='private'` (default) TETAP hanya terlihat role pemiliknya — perilaku lama tak berubah untuk skill yang belum di-share sadar. Toggle private↔shared via `POST /skills/set-visibility` (`web/main.py`); `inherited` (hasil impor skill pack, `core/skill_pack.py`) tak bisa diubah lewat situ — sudah lintas-role sejak asalnya.

Return list dict dengan field: `id`, `skill_name`, `skill_content`, `trigger_pattern`, `decay_score`, `status`.

**`mark_used(skill_id) → None`** *(async)*  
Tandai skill sebagai baru dipakai:
- Increment `use_count`
- Update `last_used_at` ke sekarang
- Tambah `skill_revive_boost` ke `decay_score` (maks 1.0)
- Jika status `'archived'` → kembalikan ke `'active'`
- WHERE menyertakan `tenant_id=?` (defense-in-depth) — tenant A tak bisa me-revive skill id milik tenant B walau id tertebak

**`mark_many_used(skill_ids) → None`** *(async)*  
Revive beberapa skill sekaligus (skill yang dipakai satu turn). **Prasyarat I2/I3:** sebelumnya `mark_used` ada tapi tak pernah dipanggil dari agent loop (revive dorman) — kini di-wire via `SkillFeedback`.

**`record_draft_outcome(skill_id, success) → dict`** *(async)* — **I2**  
`success=True` → +1 `draft_success_count`; bila ≥ `draft_promote_uses` → promote ke `active` (confidence dinaikkan ke ambang). `success=False` → reset counter. Hanya berefek pada skill `draft`.

**`maybe_run_decay_pass() → dict`** *(async)*  
Throttle gate: jika belum lewat `decay_interval_sec` sejak pass terakhir, return `{"skipped": True}` tanpa melakukan apa-apa. Dipanggil tiap turn tapi mayoritas adalah no-op.

**`_run_decay_pass() → dict`** *(async, private)*  
Jalankan decay sesungguhnya, di-scope ke `tenant_id` + `role` milik instance ini (skill tenant lain tak tersentuh):
1. UPDATE semua skill aktif: `decay_score = decay_score * POWER(0.97, hari_sejak_dipakai)` menggunakan custom function SQLite `POWER()`
2. UPDATE skill aktif yang skor-nya < threshold → `status='archived'`
3. **Draft cleanup:** UPDATE draft TUA (`> draft_stale_days`, default 14) & tak pernah terbukti (`draft_success_count=0`) → `status='archived'` (cegah menumpuk; ARSIP bukan hapus; `draft_stale_days=0` → nonaktif)

Return `{"archived": N, "drafts_archived": M}`.

---

## `memory/skill_feedback.py` — Compounding I2/I3 (jembatan antar-turn)

Menggerakkan revive (I2) & refine (I3) berdasarkan apakah turn yang memakai skill ternyata dikoreksi. Menjembatani dua turn lewat `skill_usage_pending` (AgentLoop dibuat baru tiap request).

### Kelas: `SkillFeedback`
- **`record_usage(session_id, skill_ids)`** *(async)* — post-turn: simpan skill yang disuntik ke turn ini.
- **`resolve_previous(session_id, corrected, correction_trace="")`** *(async)* — turn berikutnya: proses outcome turn lalu. `corrected=False` → revive active + promote draft (I2). `corrected=True` → reset draft + refine active (I3, gated `refine_max_per_pass`). Memproses baris pending terbaru yang belum resolved.

---

## `memory/curator.py` — Compounding I1 (Skill Curator)

Gabung/dedup skill mirip agar library tak terfragmentasi. Throttled (`curation_interval_sec`), gated (judge ≥ `curation_judge_min_confidence`) **dan** gated oleh `curation_auto` (§8, default `False`): merge yang disetujui judge hanya **diusulkan** sampai manusia klik Terapkan di `/skills` — tidak langsung mengubah skill. Anti kehilangan data (§1): loser jadi `merged` (bukan dihapus), revertible.

### Kelas: `SkillCuratorManager`
- **`maybe_run_curation_pass() → dict`** *(async)* — throttled (pola decay), dipanggil post-turn.
- **`_find_candidate_pairs() → list`** *(async, private)* — pre-filter leksikal **Jaccard token** (bukan FTS5 — FTS5 di repo ini hanya untuk `memory_l4`); pasangan dengan similarity ≥ threshold.
- **`_judge(a, b) → dict`** *(async, private)* — LLM judge tier-ringan → keputusan merge terstruktur; parse gagal/error → jangan merge (fail-safe).
- **`_pick_winner(a, b) → tuple`** *(private)* — winner = skill dengan `decay_score` tertinggi; dipakai `_merge`, `_propose`, dan `apply_pending_merge`.
- **`_merge(a, b, sim, judge)`** *(async, private)* — jalur `curation_auto=True`: terapkan merge langsung lewat `_apply_merge` (`status='applied'`).
- **`_propose(a, b, sim, judge)`** *(async, private)* — jalur default (`curation_auto=False`, §8): tulis `curation_log` dengan `status='pending'` + `merged_content` tersintesis, **tanpa** mengubah baris `skills` apa pun.
- **`_apply_merge(...)`** *(async, private)* — logika bersama: konten winner lama → `skill_versions`; winner menyerap metrik terbaik (`decay_score`/`use_count`/`confidence` MAX/SUM) + konten sintesis; loser → `merged`; catat `curation_log`.
- **`apply_pending_merge(curation_log_id) → dict`** *(async)* — terapkan satu usulan `pending` (tombol Terapkan di `/skills`, `POST /skills/apply-merge`). No-op aman bila id tak ditemukan/sudah diproses, atau skill sudah berubah sejak diusulkan (baris ditandai `reverted`).
- **`revert_last_merge() → dict`** *(async)* — pulihkan loser → active, winner ke konten/versi sebelum merge. Hanya melihat baris `status='applied'` (usulan `pending` belum mengubah apa pun). Tombol di `/skills`.

---

## `memory/user_model.py` — Compounding I5 (opsional)

Profil user naratif lintas sesi dari L2 facts, disuntik sebagai blok stabil (`## User`) di context (cocok prompt-caching). Default **nonaktif** (`user_model_enabled`); versioned + revertible; dapat dihapus (privasi §1).

### Kelas: `UserModel`
- **`get_active_profile() → str`** *(async)* — profil aktif untuk context (kosong bila nonaktif/tak ada).
- **`maybe_update() → dict`** *(async)* — throttled: rangkum L2 facts → profil baru (versioned).
- **`clear()`** *(async)* — hapus profil (privasi).

---

## `memory/search.py`

Interface FTS5 standalone yang bisa diekstrak sebagai paket terpisah.

Fungsi yang sama dengan `layers.py:archive_session + load_context[l4]` tapi dipisah agar modul ini bisa dipakai independen.

### Konstanta: `SPECIFIC_TERMS`

Sama dengan `layers.py` — term teknis yang memicu search.

### Fungsi: `fts5_query(raw) → str`

Sanitasi query bebas user menjadi query FTS5 yang aman. Query mentah memicu syntax error karena `.`, `,`, `:`, `(`, `)`, `"` adalah operator/sintaks FTS5 — sehingga query user biasa (mis. "bug login: OAuth.") membuat L4 search selalu gagal. Solusi: ekstrak token alfanumerik saja (`\w+`), bungkus tiap token dalam kutip ganda (term literal), gabung dengan `OR` untuk pencocokan parsial. Query tanpa token kata → string kosong (pemanggil harus skip MATCH). Dipakai oleh `SessionSearch.search()` dan `MemoryManager.load_context()`.

### Kelas: `SessionSearch`

**`__init__(role, db)`**

**`should_search(query) → bool`**  
Return True jika query > 3 kata atau mengandung term teknis.

**`search(query, limit=3) → list[str]`** *(async)*  
FTS5 full-text search di `memory_l4`. Query disanitasi lewat `fts5_query()` dulu — query bertanda baca kini menemukan hasil, bukan error. Return list summary sesi lama yang relevan. `try/except` tetap dipertahankan sebagai safety-net (tabel belum ada / edge case) → return `[]` + log debug.

**`archive(session_id, summary, full_content) → None`** *(async)*  
Simpan sesi ke L4. Berbeda dengan `MemoryManager.archive_session()` — **tidak idempoten** (insert langsung). Gunakan `MemoryManager.archive_session()` untuk panggilan dari agent loop.

---

## Tabel Database yang Terkait

| Tabel | Layer | Keterangan |
|---|---|---|
| `memory_l1` | L1 | Key-value; `UNIQUE(role, key)` → UPSERT |
| `memory_l2` | L2 | Fakta; diurutkan `importance DESC` |
| `skills` | L3 | Skill aktif dengan decay tracking |
| `memory_l4` | L4 | FTS5 virtual table; tidak punya UNIQUE constraint |

---

## Alur Menulis Memori (dari `agent_loop._post_turn`)

```
Tiap turn selesai:
    if turn.content:
        memory.update_checkpoint(turn.content)  → L1

    if len(history) >= archive_after_turns:
        memory.archive_session(...)              → L4 (idempoten per sesi)

    decay.maybe_run_decay_pass()                 → skills (throttled, max 1×/jam)

    if crystallizer.should_attempt(history):
        crystallizer.crystallize(...)            → skills (L3 baru)
```
