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
| `gemini_base` | `https://generativelanguage.googleapis.com` | URL base Google AI Studio (Gemini) |
| `auth_token` | `""` (kosong) | §P0 self-host auth — password shared satu-satunya user. Kosong = mode shared-secret DIMATIKAN (default, aman localhost). Isi via `OPENCLAWN_AUTH_TOKEN` di `.env` untuk self-host di VPS publik. Lihat `security/auth.py` & README § Scope and Production Posture |
| `oidc_issuer`, `oidc_client_id`, `oidc_client_secret` | `""` (kosong) | OAuth2/OIDC login (TODO.md § Prioritas 5) — mode auth TAMBAHAN, bukan pengganti shared-secret. Kosong (salah satu saja) = OIDC DIMATIKAN. Isi via `OPENCLAWN_OIDC_ISSUER`/`OPENCLAWN_OIDC_CLIENT_ID`/`OPENCLAWN_OIDC_CLIENT_SECRET`. Lihat `security/oidc.py` |
| `oidc_redirect_base` | `http://localhost:8000` | Base URL publik server, dipakai membangun `redirect_uri` callback (`{ini}/auth/callback`). HARUS diisi eksplisit (`OPENCLAWN_OIDC_REDIRECT_BASE`) untuk self-host di belakang reverse proxy/domain kustom |
| `session_secret` | acak per-boot (`secrets.token_urlsafe(32)`) | Secret HMAC untuk menandatangani cookie sesi. `from_env()` resolve: `auth_token` (bila diisi, kompatibilitas mundur) → `OPENCLAWN_SESSION_SECRET` eksplisit → fallback acak. **Operator OIDC-only (tanpa `auth_token`) HARUS mengisi `OPENCLAWN_SESSION_SECRET`** agar sesi tak hilang tiap restart server |
| `auth_active` | *(property, bukan field)* | `True` bila SALAH SATU mode auth (`auth_token` ATAU OIDC) aktif — dipakai middleware, BUKAN `bool(auth_token)` lama yang tak tahu soal OIDC-only |
| `idle_timeout_sec` | `None` (OFF) | Opt-in, TODO.md § Prioritas 1.5 — logout otomatis setelah N detik TAK aktif (beda dari `SESSION_MAX_AGE_SEC` = absolute expiry 7 hari sejak login, tetap berlaku sebagai batas atas). Isi via `OPENCLAWN_IDLE_TIMEOUT_SEC` di `.env`. Hanya berpengaruh bila auth aktif (`auth_active`). Lihat `security/auth.py` & middleware `auth_and_csrf_middleware` di `web/main.py` |
| `max_context_tokens` | `28_000` | Batas token context window |
| `max_tool_hops` | `5` | Maksimum iterasi tool loop per turn |
| `llm_max_tokens_default` | `4096` | Cap output per hop LLM saat hop TANPA tool (`tools_schema` kosong, mis. ringkas percakapan di `_maybe_compact`) |
| `llm_max_tokens_with_tools` | `8192` | Cap output per hop LLM saat hop BERTOOL (`tools_schema` terisi). Dinaikkan dari default setelah bug "No answer": model reasoning-heavy (Gemma `<think>`) butuh ruang lebih untuk merencanakan tool call. **Catatan investigasi:** menaikkan angka ini SENDIRIAN tidak selalu cukup — bila model berhenti *natural* (`done: true`) di tengah `<think>` sebelum sempat bertindak, itu bukan soal token habis (lihat kasus PM/PRD yang justru diperbaiki lewat routing ke tier lebih kuat, § `docs/roles.md` role `pm`) |
| `llm_max_retries` | `3` | Retry maksimum untuk LLM transient error |
| `approval_timeout_sec` | `120` | Detik sebelum HITL approval di-timeout (→ DENY) |
| `decay_interval_sec` | `3600` | Throttle decay pass: minimal 1 jam antar jalan |
| `skill_decay_base` | `0.97` | Base exponential decay per hari |
| `skill_archive_threshold` | `0.3` | Skor di bawah ini → skill diarsipkan |
| `skill_revive_boost` | `0.5` | Kenaikan skor saat skill dipakai lagi |
| `max_active_skills` | `8` | Maksimum skill aktif yang di-load per turn (milik role sendiri) |
| `max_shared_skills` | `3` | Skill Marketplace lintas-role (TODO.md § Prioritas 6): maksimum skill role LAIN (`visibility IN ('shared','inherited')`) yang ikut disuntik, terpisah dari `max_active_skills` |
| `confidence_threshold` | `4` | Batas bawah confidence crystallizer (1–5) |
| `archive_after_turns` | `6` | Jumlah turn sebelum sesi diarsipkan ke L4 |
| `session_history_turns` | `20` | Giliran terbaru sesi ini yang dimuat ulang ke history tiap request (batas atas; build() memangkas lagi per budget token) |
| `draft_stale_days` | `14` | Draft tua & tak terbukti (`draft_success_count=0`) diarsipkan saat decay pass; `0` = nonaktif |
| `routing_tech_keywords` | ID+EN | Keyword teknis untuk skor routing (§1.5: tak hardcoded locale; soul.toml dapat menambah per role) |
| `routing_multistep_keywords` | ID+EN | Keyword multi-langkah (analyze/compare/analisis/bandingkan…) |
| `routing_urgency_keywords` | ID+EN | Keyword urgensi (urgent/segera/deadline…) |
| `routing_language_bump` | `False` | Multibahasa lapis 2 (opt-in): naikkan tier bila script query di luar `routing_local_scripts` (model cloud lebih multibahasa). Default OFF agar tak menambah biaya |
| `routing_local_scripts` | `("latin",)` | Script (sistem tulisan) yang dianggap kuat di tier lokal — query di luar ini di-bump bila `routing_language_bump` aktif |
| `compaction_default_mode` | `"off"` | Mode compaction headroom bila `/settings` kosong: `off` (truncation, aman) / `local` / `cloud`. Opt-in karena peringkasan LLM menambah latensi & bisa membuang nuansa |
| `compaction_local_model` | `("ollama", "gemma4:e2b")` | Model lokal untuk meringkas turn lama saat mode `local` (peringkasan = ekstraktif, model kecil cukup) |
| `compaction_keep_recent` | `4` | Jumlah turn terbaru yang DIPERTAHANKAN utuh (tak diringkas) |
| `compaction_min_old_turns` | `3` | Minimal turn lama agar peringkasan dijalankan (hindari LLM call sia-sia) |
| `fallback_chain` | lihat di bawah | Urutan model jika provider utama gagal |

**Fallback chain default:**
```
gemma4:e4b (ollama) → deepseek-r1:latest (ollama) → neural-chat:latest (ollama) → gemini-2.5-flash (gemini)
```

#### Method

**`from_env() → AppConfig`** *(classmethod)*  
Baca konfigurasi dari environment variables. Variabel yang dibaca:
- `OPENCLAWN_DB` → `db_path`
- `OLLAMA_BASE` → `ollama_base`
- `ANTHROPIC_BASE` → `anthropic_base`
- `GEMINI_BASE` → `gemini_base`
- `OPENCLAWN_OIDC_ISSUER`/`OPENCLAWN_OIDC_CLIENT_ID`/`OPENCLAWN_OIDC_CLIENT_SECRET`/`OPENCLAWN_OIDC_REDIRECT_BASE` → field `oidc_*`
- `OPENCLAWN_SESSION_SECRET` → `session_secret` (hanya dipakai bila `auth_token` kosong — lihat tabel field di atas)

> **API key** (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) tidak masuk `AppConfig` — diambil saat dibutuhkan lewat `Vault` (lihat [security.md](security.md)), bukan disimpan di config.

> **`.env` di-load otomatis.** Saat modul ini di-import, `load_dotenv()` (dari `infra/env.py`) dipanggil **sebelum** `CONFIG` dibangun, sehingga key dari file `.env` tersedia di `os.environ` untuk config maupun `Vault`. Tidak perlu lagi men-source `.env` manual saat menjalankan `uvicorn`.

---

## `infra/env.py`

Pemuat `.env` minimal — tanpa dependency eksternal (`python-dotenv` sengaja tidak ditambahkan, stack final).

#### Method

**`load_dotenv(path: str | Path | None = None) → None`**  
Baca pasangan `KEY=VALUE` dari file `.env` (default: `.env` di root project) ke `os.environ`. Idempoten dan aman dipanggil berkali-kali.

- File tidak ada → diam, tidak error (`.env` opsional).
- Key yang **sudah ada** di environment tidak ditimpa — env asli (CI/deploy) selalu menang.
- Komentar (`#`) dan baris kosong diabaikan; tanda kutip pembungkus nilai di-strip.

Dipanggil sekali di puncak `infra/config.py`. Karena hampir semua entry point meng-import `infra.config`, loader berlaku untuk web server, script, maupun test.

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

## `infra/backup.py`

Backup/restore SQLite (§ production-readiness — gap dicatat di `PRODUCTION-READINESS.md` §0 & `TODO.md` § Prioritas 1.5). Dipakai lewat `scripts/backup_db.py` (cron/systemd timer) atau dipanggil langsung. Bukan `DatabaseManager` — file ini murni operasi filesystem/sqlite3 stdlib, tidak butuh koneksi shared aplikasi.

### Fungsi: `backup_database(source_path: str, backup_dir: str) → Path`

Salin `source_path` ke `backup_dir/openclawn_{timestamp}.db` (format `YYYYMMDDTHHMMSS`), return path tujuan. Pakai `sqlite3.Connection.backup()` (SQLite Online Backup API) — **bukan** `shutil.copy`/`cp` — karena aman dipanggil selagi server (koneksi WAL lain) masih hidup; hasilnya snapshot konsisten, bukan salinan byte yang berpotensi setengah-tertulis. `FileNotFoundError` bila `source_path` tidak ada.

### Fungsi: `list_backups(backup_dir: str) → list[Path]`

List file `openclawn_*.db` di `backup_dir`, terbaru dulu (sort by nama — timestamp di nama file membuat ini otomatis kronologis). Direktori tidak ada → list kosong (bukan error).

### Fungsi: `prune_old_backups(backup_dir: str, keep: int) → list[Path]`

Hapus semua backup KECUALI `keep` yang terbaru. Return list path yang dihapus. Retensi berbasis JUMLAH file (bukan umur) — lebih mudah diprediksi operator self-host dibanding "hapus yang lebih tua dari N hari" saat frekuensi backup bisa berubah.

---

## `infra/logging.py`

### Fungsi: `setup_logging() → None`
Konfigurasi `structlog` dengan JSON renderer. Dipanggil **sekali** saat startup aplikasi di `lifespan`. Output berupa JSON satu baris per event yang mudah di-parse oleh log aggregator.

Processor yang diaktifkan (urut):
- `add_log_level` — tambah field `level`
- `TimeStamper(fmt="iso")` — tambah timestamp ISO 8601
- `scrub_secrets` — **redact secret** sebelum render (§1.2 defense-in-depth)
- `JSONRenderer()` — render ke JSON

### Fungsi: `scrub_secrets(logger, method_name, event_dict) → dict`
Processor structlog yang me-redact secret SEBELUM di-render JSON, sebagai lapisan terakhir di atas `Vault` (yang menjaga credential keluar dari prompt). Bukan izin untuk log secret — tetap jangan log nilai vault.
- Field dengan nama mengandung `api_key`/`token`/`secret`/`password`/`authorization` → nilai di-`[REDACTED]` penuh.
- Nilai string berpola secret (`sk-…`, `Bearer …`, `ghp_…`, AWS/Google/Slack key) → bagian yang cocok di-redact.
- Fail-soft: error saat scrub tidak menjatuhkan logging.

### Variabel: `log`
Logger structlog siap pakai. Di-import di semua modul yang butuh logging:
```python
from infra.logging import log

log.info("event_name", key=value, ...)
log.warning("event_name", ...)
log.error("event_name", ...)
```

> **Aturan:** Setiap error di background task **HARUS** ter-log. Jangan `except: pass`.

---

## `infra/settings.py`

Setting runtime yang bisa diubah lewat halaman `/settings` **tanpa restart**. Menyimpan: (1) **override model** — memaksa semua routing ke satu `(provider, model)`, melewati `SmartRouter`; (2) **mode compaction** — `off`/`local`/`cloud` untuk peringkasan context headroom. Keduanya *pilihan sadar* untuk eksperimen/development; default-nya (router otomatis + compaction `off`) tetap berlaku jika tak diubah.

### Konstanta: `KNOWN_MODELS`

List `(provider, model, label)` untuk dropdown `/settings`. Mencakup gemma4 lokal, Claude, dan Gemini. Bukan pembatas keras — hanya saran tampilan.

### Kelas: `SettingsStore`

Murni di atas `DatabaseManager` (tabel `app_settings` key-value).

| Method | Keterangan |
|---|---|
| `get(key) → str \| None` *(async)* | Baca satu setting |
| `set(key, value) → None` *(async)* | Tulis setting; `None`/`""` menghapus baris (UPSERT) |
| `get_model_override() → tuple[str, str] \| None` *(async)* | `(provider, model)` jika override aktif, `None` jika router otomatis |
| `set_model_override(provider, model) → None` *(async)* | Set override; kirim `None`/`None` untuk kembali ke router otomatis |
| `get_compaction_mode(default="off") → str` *(async)* | Mode compaction: `off`/`local`/`cloud`. Nilai tak dikenal → fail-safe ke `default` |
| `set_compaction_mode(mode) → None` *(async)* | Set mode; nilai tak valid/`None` → kembali ke `off` |
| `get_ui_locale() → str` *(async)* | Bahasa tampilan UI (`en`/`id`, lihat `infra/i18n.py`) — **bukan** bahasa respons agent (§1.5, agent selalu ikut bahasa pesan user). Default `en`; nilai tak dikenal → fail-safe ke `en` |
| `set_ui_locale(locale) → None` *(async)* | Set locale UI; nilai tak dikenal/`None` → kembali ke `en` |

Override dianggap aktif hanya jika **provider dan model** keduanya terisi — partial (satu saja) = tetap otomatis. Mode compaction valid: `COMPACTION_MODES = ("off","local","cloud")`.

---

## `infra/workspace.py`

Batasi akses filesystem tool ke satu folder kerja (keamanan #1), plus folder kerja adaptif per-sesi (§ working directory adaptif + § user request "pindah direktori dinamis lewat chat").

### Fungsi: `resolve_in_workspace(candidate, workspace_root) → Path`

Resolve `candidate` dan pastikan tetap di dalam `workspace_root`. Me-raise `WorkspaceViolation` bila keluar (lewat `..`, absolute path, atau symlink).

### Fungsi: `effective_workspace_root(config_default) → str`

`CURRENT_WORKSPACE_ROOT` (ContextVar) bila diset, kalau tidak `config_default`.

### Fungsi: `resolve_in_current_workspace(candidate, config_default) → Path`

`resolve_in_workspace` yang root-nya ikut `effective_workspace_root` — dipakai tool file (`tools/file_ops.py` dll.) menggantikan `resolve_in_workspace(path, CONFIG.workspace_root)` langsung, agar folder kerja per-sesi otomatis terpakai tanpa mengubah signature `Tool.execute()`.

### Fungsi: `validate_workdir_candidate(raw) → tuple[str | None, str | None]`

Validasi folder kerja pilihan user SEBELUM dipakai sebagai workspace root — fail-closed: path tak lolos TIDAK PERNAH diteruskan ke ContextVar/DB. Return `(resolved_path, None)` bila valid, `(None, error_message)` bila tidak. Sengaja permisif soal LOKASI (user boleh pilih folder mana pun di mesinnya — kebalikan `resolve_in_workspace` yang membatasi ke SATU root) — hanya mengecek path benar-benar ada & direktori. Dipakai DUA jalur: field UI (`web/main.py` § `GET /workdir/check`, `/chat/stream`) dan tool `set_workdir` (`tools/workspace_tool.py`) — satu sumber kebenaran.

### Kelas: `SessionWorkspaceStore`

Folder kerja aktif per-sesi, tersimpan di DB (tabel `session_workspace`, lihat `docs/database.md`). Terpisah dari `MemoryManager` (role-scoped) agar tool `set_workdir` tak perlu import `memory/layers.py`; murni di atas `DatabaseManager`, pola sama `SettingsStore`.

| Method | Keterangan |
|---|---|
| `get(session_id) → str \| None` *(async)* | Folder aktif sesi ini, `None` jika belum pernah diset |
| `set(session_id, workdir) → None` *(async)* | UPSERT — satu baris per sesi, menimpa nilai lama |

Dibaca `AgentLoop.run()` di awal turn (bila `workspace_override` form kosong & `persist_history=True`); ditulis `SetWorkdirTool` saat tool `set_workdir` sukses.

---

## `infra/chat_sessions.py`

Metadata sesi chat single-agent untuk sidebar riwayat (§ user report: "chat selalu ke-reset", tak ada cara membuka chat baru/lanjutkan/hapus riwayat). Akar masalah lama: `session_id` di-generate ulang (uuid acak server) SETIAP kali halaman `/` di-load — tak pernah disimpan di browser (diperbaiki di `chat.js` via `localStorage`, lihat `docs/web.md`).

### Konstanta

| Nama | Nilai | Keterangan |
|---|---|---|
| `MAX_TITLE_CHARS` | `60` | Judul dipotong ke batas ini (dengan `…`) saat disimpan |
| `TITLE_INPUT_HEAD_WORDS` | `20` | Kata pertama dari pesan yang dikirim ke LLM pembuat judul |
| `TITLE_INPUT_TAIL_WORDS` | `10` | Kata terakhir dari pesan yang dikirim ke LLM pembuat judul |

### Fungsi: `truncate_for_title_prompt(message: str) → str`

Potong pesan jadi `head ... tail` bila melebihi `TITLE_INPUT_HEAD_WORDS + TITLE_INPUT_TAIL_WORDS` kata; dikirim utuh bila tidak (§ user request: pesan pertama bisa panjang, jangan bayar token generate judul untuk seluruh isinya — LLM kecil tetap dapat konteks AWAL dan AKHIR, karena topik kadang baru jelas di akhir paragraf). Murni fungsi string tanpa I/O.

### Kelas: `ChatSessionStore`

Murni di atas `DatabaseManager` (tabel `chat_sessions`, lihat `docs/database.md`).

**`__init__(db, tenant_id="default")`** — Multi-Tenant (TODO.md § Prioritas 5, WIRED PENUH): `tenant_id` opsional, default `'default'`. Instance ini terikat SATU tenant — semua method di bawah men-scope ke tenant tersebut.

| Method | Keterangan |
|---|---|
| `ensure_created(session_id, role) → None` *(async)* | Daftarkan sesi baru (`INSERT OR IGNORE` — idempoten, tak menimpa title/waktu sesi yang sudah ada), menyertakan `tenant_id` instance. Dipanggil `/chat/stream` SEBELUM turn jalan, agar sesi muncul di sidebar walau turn pertama gagal/timeout |
| `touch(session_id) → None` *(async)* | Perbarui `updated_at` — dipanggil tiap turn (`AgentLoop._post_turn`) agar urutan sidebar (terbaru dulu) mencerminkan aktivitas terakhir |
| `set_title(session_id, title) → None` *(async)* | Simpan judul; strip tanda kutip pembungkus (LLM kadang membungkus jawaban dengan `"..."`) & potong ke `MAX_TITLE_CHARS` |
| `has_title(session_id) → bool` *(async)* | Cek apakah sesi sudah punya judul — gate agar generate judul hanya sekali (turn pertama) |
| `list_active(limit=200) → list[dict]` *(async)* | Sesi MILIK TENANT INI yang belum dihapus, terbaru dulu — mentah (tak dikelompokkan; `web/main.py` § `GET /chat-sessions` yang menghitung `bucket` waktu). Sesi tenant lain tak pernah muncul |
| `soft_delete(session_id) → None` *(async)* | Hapus dari sidebar (`deleted_at` terisi — metadata tetap ada untuk audit trail), TAPI `session_turns` & `session_workspace` terkait dihapus FISIK (user minta "hapus", isi percakapan harus benar hilang). WHERE menyertakan `tenant_id=?` (defense-in-depth) — tenant A tak bisa menghapus sesi tenant B walau tahu `session_id`-nya |

Judul di-generate `AgentLoop._generate_session_title` (dipanggil `_post_turn` di turn pertama, gated `has_title`) via `compaction_local_model` (gemma4:e2b) — model kecil yang sama dipakai `_maybe_compact`, konsisten & gratis (lokal). Fail-safe (§1.3): LLM/parsing gagal → sesi tetap tanpa judul (sidebar fallback ke `"New chat"`), tak menjatuhkan turn.

---

## `infra/users.py` — Multi-user + RBAC (TODO.md § Prioritas 5, revisi eksplisit CLAUDE.md §7)

Multi-user SUNGGUHAN per tenant (bukan lagi "satu identitas per deployment") —
revisi eksplisit CLAUDE.md §7, disetujui owner (tabel penuh, bukan role tunggal
per identitas seperti pola bukti-konsep sub-item Prioritas 5 lain). Detail
lengkap di `docs/security.md` § `infra/users.py` (tempat modul ini didokumentasikan
utuh, karena erat kaitannya dengan `security/auth.py`/`security/oidc.py`) dan
`docs/database.md` § `users`. Ringkasan:

- `SHARED_SECRET_SUBJECT`, `ACCESS_ROLES = ("admin", "member", "viewer")`.
- `role_at_least(access_role, minimum) → bool` — hierarki `viewer < member < admin`.
- `UserStore(db, tenant_id="default")`: `upsert_on_login`, `set_access_role`,
  `get_by_subject`, `get_by_id`, `list_users` — semua `*(async)*`.

---

## `infra/manifest.py` — clawn.yaml (TODO.md § Prioritas 3)

Manifest deklaratif tim/role DI ATAS `soul.toml` — operator menulis policy sekali per tool di `clawn.yaml`, bukan menyunting `[policy.*]` manual di tiap `soul.toml`. Dijalankan via `scripts/apply_manifest.py`, bukan otomatis saat startup (perubahan config harus sadar/eksplisit, bukan tersirat tiap restart).

**Dependency baru: PyYAML** (disetujui owner secara eksplisit, lihat `pyproject.toml`) — HANYA untuk parsing `clawn.yaml`. Penulisan ke `soul.toml` TETAP text-based section replace (bukan serializer TOML generik) — `system_prompt` multi-baris kompleks harus tetap byte-identik, hanya blok `[policy.*]` yang dicari & diganti.

### Exception: `ManifestError`

Kegagalan memuat/menerapkan manifest — pesan jelas ke operator, bukan traceback generik. Dilempar untuk: file tak ada, YAML tak valid, skema tanpa key `team` di root, atau role yang disebut manifest tapi `soul.toml`-nya tak ditemukan (tidak diam-diam membuat file baru).

### Fungsi: `load_manifest(manifest_path) → dict`

Baca & validasi `clawn.yaml`. Raise `ManifestError` untuk kondisi di atas.

### Fungsi: `generate_policy_toml_block(policy) → str`

Render dict `{tool_name: {deny_if: [...], approval_required_if: [...]}}` jadi blok TOML `[policy.<tool_name>]` siap disisipkan. Angka ditulis TANPA quote, string DENGAN quote (`_toml_value`) — penting karena kondisi numerik (`op: "gt"`, `value: 300`) harus banding angka, bukan string. Dict kosong → string kosong.

### Fungsi: `apply_manifest(manifest_path, roles_dir="roles") → list[str]`

Terapkan `clawn.yaml` ke `soul.toml` tiap role yang disebut di `team:`. Return list role yang benar-benar diubah.

- Role tanpa key `policy` di manifest-nya → **di-skip** (no-op untuk role itu, bukan menghapus policy yang mungkin sudah ada dari sumber lain).
- Role yang disebut manifest tapi `soul.toml`-nya tidak ada di `roles_dir` → `ManifestError` (bukan membuat file baru diam-diam — itu keputusan operator).
- Role yang **tidak disebut** manifest sama sekali → `soul.toml`-nya tidak disentuh (opt-in per-role, bukan all-or-nothing).
- Idempoten: menjalankan `apply_manifest` berkali-kali dengan manifest sama tidak menumpuk blok `[policy.*]` duplikat — blok lama dihapus (regex `_POLICY_SECTION_RE`) sebelum blok baru disisipkan.

### Skema `clawn.yaml`

```yaml
team:
  pm:
    policy:
      pdf_write:
        approval_required_if:
          - field: content
            op: contains
            value: confidential
  dev:
    policy:
      shell_run:
        approval_required_if:
          - op: always
```

**Catatan scope:** field `model`/`approval` top-level di skema PDF asli (`team.<role>.model`, `team.<role>.approval`) dicatat di manifest tapi BELUM di-generate ke `soul.toml` — hanya `policy` yang diproses saat ini. Perluasan lanjutan dilacak terpisah di `TODO.md`.
