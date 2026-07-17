# Jalur SQLite → PostgreSQL (TODO.md § Prioritas 5)

**Status: opsi terdokumentasi, BUKAN migrasi wajib.** SQLite (aiosqlite, WAL
mode) tetap DEFAULT untuk deployment self-hosted single-organization — nilai
jual data sovereignty (CLAUDE.md §7, `KESIMPULAN.md` §2.4). Dokumen ini ada
supaya opsi Postgres **tidak diblok arsitektur** untuk deployment yang butuh
skala lintas-proses/concurrent-write tinggi, bukan untuk mendorong migrasi.

Skema `tenant_id` (TODO.md § Prioritas 5, lihat `docs/database.md` § Multi-Tenant)
sengaja kompatibel dengan model multi-tenant standar (satu kolom filter per
baris, bukan skema-per-tenant atau database-per-tenant) — migrasi ke Postgres
adalah perubahan `DatabaseManager`/driver, **bukan perubahan skema logis**.

## Kapan (dan kapan TIDAK) migrasi ini masuk akal

**Migrasi masuk akal bila:**
- Butuh **concurrent write** tinggi lintas proses — SQLite WAL mode
  mengizinkan banyak reader bersamaan dengan satu writer, tapi write
  serialized secara global. Single-instance/single-writer OpenCLAWN saat ini
  jarang menabrak batas ini (§ `PRODUCTION-READINESS.md` §8), tapi deployment
  multi-tenant dengan banyak organisasi aktif bersamaan bisa.
- Butuh **horizontal scaling** proses `web/main.py` (banyak instance di
  belakang load balancer) — SQLite adalah file lokal, tak bisa dibagi aman
  lintas proses di host berbeda tanpa NFS/replikasi tambahan (tak
  direkomendasikan). Postgres adalah server terpisah, wajar diakses banyak
  instance aplikasi.
- Butuh **backup/replikasi tingkat-enterprise** (point-in-time recovery,
  streaming replication, managed service seperti RDS/Cloud SQL) di luar
  `infra/backup.py` (SQLite Online Backup API, cukup untuk single-instance).

**TIDAK perlu migrasi bila:** self-host single-organization, single-instance,
volume tulis wajar (turn chat, decay pass per jam, dst — bukan ribuan write/detik).
SQLite WAL mode + `infra/backup.py` sudah cukup, dan tetap keunggulan sovereignty
(file lokal, tak butuh server DB terpisah untuk dioperasikan/diamankan).

## Ringkasan pekerjaan

| Area | Effort | Kenapa |
|---|---|---|
| `DatabaseManager` (`infra/database.py`) | Sedang | Ganti driver `aiosqlite`→`asyncpg`/`psycopg`, connection pool alih-alih satu koneksi shared |
| `migrations/001_initial.sql` | Sedang | Dialek SQL berbeda (lihat tabel translasi di bawah), TAPI struktur tabel/kolom/tenant_id TIDAK berubah |
| `POWER()`/`julianday()` (skill decay) | Kecil | Postgres punya `POWER()` bawaan; `julianday()` diganti `EXTRACT(EPOCH FROM ...)` |
| `memory_l4` (FTS5 full-text search) | Sedang-Besar | SQLite FTS5 → Postgres `tsvector`/`tsquery` + GIN index, sintaks query beda total |
| Migrasi kolom otomatis (`_ADDED_COLUMNS`, `_rebuild_tables_for_multi_tenant`) | Kecil | Pola idempoten (`PRAGMA table_info`) diganti query `information_schema.columns` — logika sama, hanya introspection berbeda |
| Kode query lain (SELECT/INSERT/UPDATE biasa) | Minimal | Sebagian besar SQL proyek ini portable (placeholder `?`→`$1`/`%s` tergantung driver, tanpa fitur SQLite eksotis) |

## Translasi dialek SQL yang dibutuhkan

### 1. Tipe kolom & auto-increment

```sql
-- SQLite (sekarang)
id INTEGER PRIMARY KEY                    -- auto-increment implisit (rowid alias)
tenant_id TEXT DEFAULT 'default'
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

-- PostgreSQL
id SERIAL PRIMARY KEY                     -- atau BIGSERIAL / IDENTITY (PG 10+)
tenant_id TEXT DEFAULT 'default'          -- tak berubah
created_at TIMESTAMPTZ DEFAULT NOW()      -- TIMESTAMPTZ untuk timezone-aware
```

### 2. `POWER()` dan `julianday()` — exponential decay (`memory/skill_decay.py`)

```sql
-- SQLite (sekarang) — POWER() didaftarkan manual, SQLite tak punya bawaan
-- (infra/database.py::conn(), _conn.create_function("POWER", 2, lambda b, e: b**e))
UPDATE skills
SET decay_score = decay_score * POWER(?, julianday('now') - julianday(COALESCE(last_used_at, created_at)))
WHERE tenant_id=? AND role=? AND status='active';

-- PostgreSQL — POWER() bawaan, julianday diganti EXTRACT(EPOCH ...)/86400 (hari)
UPDATE skills
SET decay_score = decay_score * POWER($1, EXTRACT(EPOCH FROM (NOW() - COALESCE(last_used_at, created_at))) / 86400)
WHERE tenant_id=$2 AND role=$3 AND status='active';
```

`DatabaseManager.conn()` tak perlu lagi mendaftarkan custom function `POWER` untuk backend Postgres — baris `_conn.create_function("POWER", ...)` jadi no-op/dilewati.

### 3. `memory_l4` — FTS5 → `tsvector` (§ perubahan paling besar)

```sql
-- SQLite (sekarang) — virtual table FTS5
CREATE VIRTUAL TABLE memory_l4 USING fts5(
    role, session_id, summary, full_content, created_at UNINDEXED
);
-- Query: SELECT summary FROM memory_l4 WHERE role=? AND memory_l4 MATCH ? ORDER BY rank LIMIT ?

-- PostgreSQL — kolom biasa + generated tsvector + GIN index
CREATE TABLE memory_l4 (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT DEFAULT 'default',
    role TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    full_content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', summary || ' ' || full_content)) STORED
);
CREATE INDEX idx_memory_l4_search ON memory_l4 USING GIN(search_vector);
-- Query: SELECT summary FROM memory_l4 WHERE role=$1 AND search_vector @@ plainto_tsquery('english', $2)
--        ORDER BY ts_rank(search_vector, plainto_tsquery('english', $2)) DESC LIMIT $3
```

`memory/search.py::fts5_query()` (sanitasi query untuk sintaks MATCH SQLite —
lihat `docs/memory.md`) perlu fungsi setara untuk `plainto_tsquery`/`websearch_to_tsquery`
Postgres (lebih permisif soal karakter khusus, kemungkinan butuh sanitasi lebih
sedikit — tapi tetap diverifikasi, bukan diasumsikan aman).

### 4. Migrasi kolom otomatis (`_ADDED_COLUMNS`, `infra/database.py`)

```python
# SQLite (sekarang) — introspection via PRAGMA
async with db.execute(f"PRAGMA table_info({table})") as cursor:
    existing = {row[1] async for row in cursor}

# PostgreSQL — introspection via information_schema
async with db.execute(
    "SELECT column_name FROM information_schema.columns WHERE table_name=$1", (table,)
) as cursor:
    existing = {row[0] async for row in cursor}
```

Logika idempoten (cek kolom ada sebelum `ALTER TABLE ADD COLUMN`) tak berubah — hanya query introspection-nya.

### 5. Placeholder parameter

SQLite/`aiosqlite` pakai `?` untuk parameter positional; `asyncpg` pakai `$1, $2, ...`, `psycopg`/`psycopg2` pakai `%s`. Ini menyentuh **setiap** query berparameter di seluruh proyek — perubahan mekanis (regex/AST rewrite atau query builder tipis), bukan perubahan logika.

### 6. `INSERT ... ON CONFLICT`

Sintaksnya HAMPIR identik — SQLite dan Postgres sama-sama pakai `ON CONFLICT(cols) DO UPDATE/DO NOTHING` (SQLite mengadopsi sintaks upsert Postgres). Contoh dari `memory/layers.py`:

```sql
-- Sama persis di kedua dialek, hanya placeholder yang beda
INSERT INTO memory_l1 (role, key, value) VALUES (?, 'last_summary', ?)
ON CONFLICT(tenant_id, role, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
```

## Yang TIDAK berubah

- **Struktur tabel/kolom/relasi** — skema logis (termasuk seluruh desain `tenant_id` Multi-Tenant, `docs/database.md`) tetap sama persis.
- **`DatabaseManager` sebagai satu-satunya jalur akses DB** — modul lain (`ChatSessionStore`, `SkillDecayManager`, `UserStore`, dst) memanggil `db.execute`/`db.fetchall`/`db.fetchone`, bukan driver langsung — migrasi backend terisolasi di satu file (`infra/database.py`).
- **Semua modul RBAC/OIDC/multi-tenant** (`infra/users.py`, `security/oidc.py`, `infra/chat_sessions.py`, `memory/skill_decay.py`) — didesain generik terhadap `DatabaseManager`, tak ada asumsi SQLite spesifik di luar 4 titik dialek di atas.

## Yang TIDAK termasuk scope dokumen ini

- Connection pooling / sizing untuk beban produksi Postgres — bergantung
  deployment nyata (jumlah instance, concurrent user), tak bisa
  digeneralisasi di sini.
- Strategi migrasi data DARI SQLite existing KE Postgres kosong (`pgloader`
  atau dump/restore manual) — di luar scope "opsi tak diblok arsitektur";
  relevan hanya saat ada deployment nyata yang benar-benar bermigrasi.
- Perubahan `pyproject.toml` (`asyncpg`/`psycopg` sebagai dependency baru) —
  butuh persetujuan eksplisit owner saat implementasi nyata dimulai (CLAUDE.md §8), bukan bagian dari dokumentasi opsi ini.

Implementasi aktual (bukan sekadar dokumentasi jalur) ditunda sampai ada
kebutuhan pilot nyata yang memvalidasinya (CLAUDE.md §8) — dokumen ini
membuktikan jalurnya **ada dan dipetakan**, bukan mengerjakannya di muka.
