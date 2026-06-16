# `infra/` — Fondasi Infrastruktur

Modul ini dibangun **pertama** sebelum semua modul lain. Semua modul bergantung pada `infra/`. Jangan tulis modul lain sebelum `infra/` jalan dan tertest.

---

## `infra/config.py`

### Kelas: `AppConfig`

Dataclass **frozen** (immutable setelah dibuat) yang menyimpan seluruh konfigurasi global. Semua angka ajaib ada di sini — tidak tersebar di kode.

```python
CONFIG = AppConfig.from_env()  # singleton global, di-inject ke semua modul
```

#### Field

| Field | Default | Keterangan |
|---|---|---|
| `db_path` | `data/openclawn.db` | Path file SQLite |
| `ollama_base` | `http://localhost:11434` | URL base Ollama |
| `anthropic_base` | `https://api.anthropic.com` | URL base Anthropic API |
| `max_context_tokens` | `28_000` | Batas token context window |
| `max_tool_hops` | `5` | Maksimum iterasi tool loop per turn |
| `llm_max_retries` | `3` | Retry maksimum untuk LLM transient error |
| `approval_timeout_sec` | `120` | Detik sebelum HITL approval di-timeout (→ DENY) |
| `decay_interval_sec` | `3600` | Throttle decay pass: minimal 1 jam antar jalan |
| `skill_decay_base` | `0.97` | Base exponential decay per hari |
| `skill_archive_threshold` | `0.3` | Skor di bawah ini → skill diarsipkan |
| `skill_revive_boost` | `0.5` | Kenaikan skor saat skill dipakai lagi |
| `max_active_skills` | `8` | Maksimum skill aktif yang di-load per turn |
| `confidence_threshold` | `4` | Batas bawah confidence crystallizer (1–5) |
| `archive_after_turns` | `6` | Jumlah turn sebelum sesi diarsipkan ke L4 |
| `fallback_chain` | lihat di bawah | Urutan model jika provider utama gagal |

**Fallback chain default:**
```
gemma4:12b (ollama) → gemma4:e4b (ollama) → gemma4:e2b (ollama) → claude-haiku-4-5-20251001 (anthropic)
```

#### Method

**`from_env() → AppConfig`** *(classmethod)*  
Baca konfigurasi dari environment variables. Variabel yang dibaca:
- `OPENCLAWN_DB` → `db_path`
- `OLLAMA_BASE` → `ollama_base`
- `ANTHROPIC_BASE` → `anthropic_base`

---

## `infra/database.py`

### Kelas: `DatabaseManager`

Memegang **satu koneksi shared** per proses (`aiosqlite`). Di-pass ke semua modul via dependency injection — jangan buat koneksi baru di tiap metode.

Fitur khusus saat koneksi pertama dibuka:
- WAL mode diaktifkan (`PRAGMA journal_mode=WAL`) untuk performa write concurrrent
- Foreign keys diaktifkan (`PRAGMA foreign_keys=ON`)
- Custom function `POWER(base, exp)` didaftarkan — SQLite tidak punya bawaan, dibutuhkan untuk exponential decay di `skill_decay.py`

#### Method

**`conn() → aiosqlite.Connection`** *(async)*  
Kembalikan koneksi aktif. Buka dan konfigurasi koneksi jika belum ada (lazy init).

**`execute(sql, params=()) → aiosqlite.Cursor`** *(async)*  
Eksekusi SQL DML (INSERT/UPDATE/DELETE) dan commit. Return cursor (untuk `lastrowid`, `rowcount`).

**`fetchall(sql, params=()) → list[dict]`** *(async)*  
Eksekusi SELECT dan kembalikan semua baris sebagai list of dict.

**`fetchone(sql, params=()) → dict | None`** *(async)*  
Eksekusi SELECT dan kembalikan satu baris sebagai dict, atau `None` jika tidak ada.

**`run_migration(sql_path: str) → None`** *(async)*  
Baca file SQL dari `sql_path` dan jalankan sebagai script (untuk migration). Dipanggil saat startup di `web/main.py`.

**`close() → None`** *(async)*  
Tutup koneksi. Dipanggil saat shutdown.

---

## `infra/logging.py`

### Fungsi: `setup_logging() → None`
Konfigurasi `structlog` dengan JSON renderer. Dipanggil **sekali** saat startup aplikasi di `lifespan`. Output berupa JSON satu baris per event yang mudah di-parse oleh log aggregator.

Processor yang diaktifkan:
- `add_log_level` — tambah field `level`
- `TimeStamper(fmt="iso")` — tambah timestamp ISO 8601
- `JSONRenderer()` — render ke JSON

### Variabel: `log`
Logger structlog siap pakai. Di-import di semua modul yang butuh logging:
```python
from infra.logging import log

log.info("event_name", key=value, ...)
log.warning("event_name", ...)
log.error("event_name", ...)
```

> **Aturan:** Setiap error di background task **HARUS** ter-log. Jangan `except: pass`.
