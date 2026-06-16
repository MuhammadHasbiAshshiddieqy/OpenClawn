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

L4 hanya di-query jika query > 3 kata **atau** mengandung term teknis spesifik. Ini hemat query SQLite untuk percakapan pendek/casual.

**`update_checkpoint(summary: str) → None`** *(async)*  
Tulis/update L1 key `"last_summary"` dengan konten terbaru (maks 500 karakter). Operasi UPSERT — tidak duplikasi.

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

**`__init__(role, db, config)`**  
Cache `_last_decay_ts` untuk throttle.

**`get_active_skills(query) → list[dict]`** *(async)*  
Ambil skill aktif yang relevan dengan query (trigger_pattern match). Urutkan berdasarkan `decay_score DESC, use_count DESC`. Maks `config.max_active_skills` skill.

Return list dict dengan field: `id`, `skill_name`, `skill_content`, `trigger_pattern`, `decay_score`.

**`mark_used(skill_id) → None`** *(async)*  
Tandai skill sebagai baru dipakai:
- Increment `use_count`
- Update `last_used_at` ke sekarang
- Tambah `skill_revive_boost` ke `decay_score` (maks 1.0)
- Jika status `'archived'` → kembalikan ke `'active'`

**`maybe_run_decay_pass() → dict`** *(async)*  
Throttle gate: jika belum lewat `decay_interval_sec` sejak pass terakhir, return `{"skipped": True}` tanpa melakukan apa-apa. Dipanggil tiap turn tapi mayoritas adalah no-op.

**`_run_decay_pass() → dict`** *(async, private)*  
Jalankan decay sesungguhnya:
1. UPDATE semua skill aktif: `decay_score = decay_score * POWER(0.97, hari_sejak_dipakai)` menggunakan custom function SQLite `POWER()`
2. UPDATE skill yang skor-nya < threshold → `status='archived'`

Return `{"archived": N}` dengan N = jumlah skill yang baru diarsipkan.

---

## `memory/search.py`

Interface FTS5 standalone yang bisa diekstrak sebagai paket terpisah.

Fungsi yang sama dengan `layers.py:archive_session + load_context[l4]` tapi dipisah agar modul ini bisa dipakai independen.

### Konstanta: `SPECIFIC_TERMS`

Sama dengan `layers.py` — term teknis yang memicu search.

### Kelas: `SessionSearch`

**`__init__(role, db)`**

**`should_search(query) → bool`**  
Return True jika query > 3 kata atau mengandung term teknis.

**`search(query, limit=3) → list[str]`** *(async)*  
FTS5 full-text search di `memory_l4`. Return list summary sesi lama yang relevan. Jika FTS5 syntax error atau tabel belum ada → return `[]` dan log warning (graceful degradation).

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
