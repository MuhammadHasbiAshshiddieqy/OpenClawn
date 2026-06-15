# CLAUDE.md — Panduan Implementasi OpenCLAWN

> File ini adalah **panduan operasional** untuk agent coding (Claude Code / Sonnet / model lain) yang mengimplementasikan OpenCLAWN.
>
> **Cara pakai:** Bawa file ini + `openclawn-core-spec-v0.3.md` ke repository baru. Dua file ini cukup untuk memberi konteks penuh tanpa membawa riwayat percakapan. Baca **kedua** file sebelum menulis kode apa pun.
>
> **Aturan emas:** Spec (`openclawn-core-spec-v0.3.md`) adalah *sumber kebenaran* untuk APA yang dibangun. File ini (`CLAUDE.md`) adalah sumber kebenaran untuk BAGAIMANA membangunnya — konvensi, urutan kerja, dan hal yang tidak boleh dilanggar.

---

## 0. TL;DR untuk agent yang baru masuk

Kamu sedang membangun **OpenCLAWN**: framework agent AI yang ringan, aman, self-improving, dan multi-role. Python 3.12, FastAPI + HTMX, SQLite, hybrid LLM (Ollama lokal + Claude API).

Yang membuat proyek ini berbeda adalah **4 inovasi inti**:
1. **Routing audit + self-calibration** — catat setiap keputusan routing + apakah terbukti tepat
2. **Skill decay** — skill yang jarang dipakai memudar dan ter-arsip
3. **Confidence-gated crystallization** — agent menilai kualitas solusinya sebelum menyimpannya sebagai skill
4. **Role output contracts** — handoff antar role tervalidasi dengan Pydantic

Empat inovasi ini bukan fitur tambahan — mereka adalah inti dari nilai proyek. Jangan pernah memangkasnya demi "menyederhanakan".

**Mulai dari Sprint 0** di §21 spec. Jangan loncat. Bangun fondasi (`infra/`) dulu, baru yang lain.

---

## 1. Prinsip yang tidak boleh dilanggar

Urut berdasarkan prioritas. Jika dua prinsip bertabrakan, yang lebih atas menang.

1. **Keamanan dulu.** `code_run` HARUS berjalan di dalam Docker sandbox (`--network none`, `--read-only`, non-root, timeout). Tidak ada eksekusi kode di host. Tidak pernah. Lihat spec §16.
2. **Credential tidak pernah masuk context/prompt.** Hanya diinjeksi saat outbound request via `Vault`. Jangan pernah log API key. Jangan pernah taruh di tabel DB.
3. **Setiap dependency eksternal punya kegagalan yang anggun.** LLM call → retry + fallback chain. Ollama offline ≠ agent mati. Lihat spec §8.
4. **Token-first.** Sebelum menambah apa pun ke context window, tanya: apakah ini perlu? Target < 28K token. Aktifkan prompt caching untuk bagian stabil.
5. **Tidak ada hardcoded domain/locale.** OpenCLAWN harus netral. Tidak ada "ServisIn", tidak ada "Depok", tidak ada Bahasa Indonesia yang dipaksakan di core. Locale via field `locale`, bukan di kode.
6. **Setiap inovasi = modul yang bisa diekstrak.** Tulis `skill_decay.py`, `audit.py`, `crystallizer.py`, `contracts.py` sedemikian rupa sehingga suatu hari bisa dijadikan paket terpisah. Jangan bocorkan ketergantungan spesifik OpenCLAWN ke dalamnya selain lewat interface yang jelas (`DatabaseManager`).

---

## 2. Konvensi kode (WAJIB diikuti)

### Bahasa & gaya
- **Python 3.12+**, gunakan fitur modern: `match`, `|` union types, `list[str]` bukan `List[str]`.
- **Type hints di semua fungsi publik.** Tidak ada `Any` kecuali benar-benar terpaksa.
- **`async`/`await` di semua I/O.** DB, HTTP, file besar. Tidak ada blocking call di event loop.
- **Format dengan `ruff format`. Lint dengan `ruff check`.** Jalankan sebelum menganggap tugas selesai.
- **Docstring singkat** untuk setiap kelas dan fungsi non-trivial. Bahasa Indonesia boleh, English boleh — konsisten dalam satu file.

### Penamaan
- File & modul: `snake_case.py`
- Kelas: `PascalCase`
- Fungsi & variabel: `snake_case`
- Konstanta: `UPPER_SNAKE_CASE` di puncak modul
- Private: prefix `_` (mis. `_load_soul_once`)

### Struktur
- Satu kelas utama per file modul. File pendukung kecil OK.
- Import absolut dari root proyek: `from infra.database import DatabaseManager`. Bukan relative.
- Konstanta konfigurasi di puncak file, bukan magic number tersebar.

### Komentar
- Jelaskan **mengapa**, bukan **apa**. Kode yang baik sudah menjelaskan "apa".
- Untuk setiap perbaikan dari audit, tinggalkan komentar referensi: `# Audit #4: evaluator minimal setara generator`. Ini memudahkan pelacakan.

---

## 3. Aturan khusus per modul

### `infra/` — bangun PERTAMA
Semua modul lain bergantung pada ini. Jangan tulis modul lain sebelum `infra/` jalan dan tertest.
- `config.py`: `AppConfig` adalah `frozen=True` dataclass. Semua angka ajaib (timeout, threshold, max tokens) ada di sini, bukan tersebar.
- `database.py`: `DatabaseManager` memegang SATU koneksi shared. Daftarkan `POWER(base, exp)` sebagai custom function (SQLite tidak punya bawaan — dibutuhkan exponential decay). Aktifkan WAL mode.
- `logging.py`: structlog JSON. Setiap error di background task HARUS ter-log.

### `core/llm_client.py` — bangun KEDUA
Ini fondasi semua interaksi LLM. Jangan ada modul yang call LLM langsung; semua lewat sini.
- `stream_with_fallback()` adalah satu-satunya entry point publik.
- Health check Ollama sebelum pakai. Anthropic asumsikan up, andalkan retry.
- Retry hanya untuk `httpx.HTTPError` (transient). Jangan retry error logika.
- Prompt caching: bungkus system prompt dengan `cache_control: ephemeral`.
- **Jangan pakai SDK Anthropic atau OpenAI.** Raw httpx. Ini disengaja untuk transparansi audit.

### `core/router.py` — soul-aware
- Router membaca `soul.toml` saat `__init__`, bukan tiap request.
- `upgrade_keywords` dari soul menambah skor (+3). `prefer_local` menggeser threshold (+1, bertahan lebih lama di Ollama).
- Simpan SEMUA 8 dimensi di `RouteDecision.dimensions` — auditor butuh ini.

### `core/audit.py` — Inovasi 1
- Catat keputusan SEBELUM call LLM (`log_decision`), update hasil SESUDAH (`finalize`).
- `check_correction` dipanggil di AWAL turn berikutnya — mendeteksi apakah turn sebelumnya dikoreksi user.
- `calibration_report` adalah cara kita tahu router perlu di-tune. Jangan dihapus.

### `memory/skill_decay.py` — Inovasi 2
- Decay EKSPONENSIAL: `score = score * (0.97 ^ hari_sejak_dipakai)`. Bukan linear.
- `maybe_run_decay_pass` di-throttle (default 1 jam). Dipanggil tiap turn tapi mayoritas no-op. Jangan jalankan decay penuh tiap turn.
- Skill yang dipakai lagi → `mark_used` → revive (status kembali active, score naik).

### `core/crystallizer.py` — Inovasi 3
- **KRITIS:** evaluator minimal setara generator. Lihat map `EVALUATOR_FOR`. Solusi Sonnet TIDAK BOLEH dinilai 7B. Ini bukan opsional — ini yang membuat inovasi ini valid.
- Confidence < 4 ATAU ada critical_gaps → status `draft`, bukan `active`. Draft tidak masuk auto-context.
- Self-evaluation harus minta output JSON ketat. Parse gagal → fail-safe ke confidence rendah.

### `roles/` — Inovasi 4
- Setiap contract adalah Pydantic `BaseModel`. Validasi di `RoleNegotiator._validate`.
- Output tidak valid → `validation_ok = 0`, simpan raw untuk debugging. Jangan crash.

### `tools/` — keamanan
- Setiap Tool punya `requires_approval: bool`. `code_run` → `True` selalu.
- `code_run` HANYA lewat `DockerSandbox`. Tidak ada `exec()`, `eval()`, `subprocess` langsung ke host.

### `security/` 
- `Shield` adalah lapisan kosmetik. Pertahanan utama = container isolation. Jangan beri rasa aman palsu di komentar atau dokumentasi.
- NFKD normalize sebelum regex (cegah homoglyph).
- `ApprovalGate`: research phase auto-approve + log. Sprint 3 ganti ke interaktif (Future yang di-resolve Web UI).

---

## 4. Urutan kerja (jangan menyimpang)

Ikuti roadmap Sprint di spec §21. Dalam setiap sprint, urutannya:

1. **Tulis test dulu** (atau minimal skeleton test) untuk komponen yang akan dibangun.
2. **Implementasi** komponen.
3. **Jalankan test** sampai hijau.
4. **`ruff format` + `ruff check`** sampai bersih.
5. **Verifikasi manual** via query SQLite (lihat "Verifikasi" di spec §22) jika menyentuh DB.
6. **Centang** checklist sprint di spec.

Jangan menumpuk banyak komponen sekaligus tanpa test di antaranya. Satu komponen → hijau → lanjut.

### Definition of Done untuk satu komponen
- [ ] Type hints lengkap
- [ ] Test ada dan hijau (pakai DB `:memory:`, mock LLM)
- [ ] `ruff` bersih
- [ ] Komentar referensi audit jika relevan
- [ ] Tidak ada hardcoded secret/domain/locale
- [ ] Error path ditangani (tidak ada `except: pass` tanpa log)

---

## 5. Testing — aturan ketat

- **Framework:** pytest + pytest-asyncio (`asyncio_mode = "auto"` sudah di pyproject).
- **DB:** selalu `:memory:` untuk test. Jangan sentuh `data/openclawn.db` asli.
- **LLM:** SELALU mock. Test tidak boleh memanggil Ollama atau Claude sungguhan. Pakai `unittest.mock.AsyncMock`.
- **Satu file test per inovasi** minimal: `test_router.py`, `test_skill_decay.py`, `test_crystallizer.py`, `test_contracts.py`, `test_fallback.py`.
- **Test yang wajib ada** (dari spec §20):
  - Router: soul upgrade_keyword menaikkan kompleksitas
  - Router: prefer_local menahan query di Ollama
  - Fallback: Ollama down → turun ke fallback
  - Crystallizer: evaluator tidak lebih lemah dari generator
  - Skill decay: skill tak terpakai memudar; skill dipakai lagi revive
  - Contracts: output valid lolos, output buruk ditolak tanpa crash

### Pola test async
```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_sesuatu():
    db = DatabaseManager(AppConfig(db_path=":memory:"))
    # ... setup schema, jalankan, assert
    await db.close()
```

---

## 6. Yang TIDAK boleh dilakukan

- ❌ Jangan pakai SDK Anthropic/OpenAI. Raw httpx only.
- ❌ Jangan pakai LangChain, LlamaIndex, atau framework agent besar. Ini proyek minimal.
- ❌ Jangan pakai `localStorage`/`sessionStorage` di Web UI (tidak relevan di sini, tapi juga tidak perlu state browser).
- ❌ Jangan jalankan `code_run` di luar Docker sandbox.
- ❌ Jangan hardcode domain ("ServisIn") atau locale (Bahasa Indonesia) di core.
- ❌ Jangan buat koneksi DB baru di tiap metode — pakai `DatabaseManager`.
- ❌ Jangan pakai recursion untuk tool loop — iterative `while`.
- ❌ Jangan `asyncio.create_task` tanpa `add_done_callback` untuk error logging.
- ❌ Jangan evaluasi solusi dengan model lebih lemah dari generatornya.
- ❌ Jangan hapus atau "sederhanakan" salah satu dari 4 inovasi inti.
- ❌ Jangan ubah nama model Claude jadi versi lama. `claude-haiku-4-5-20251001` dan `claude-sonnet-4-6` sudah benar dan terverifikasi.

---

## 7. Pengetahuan domain yang sudah final (jangan ditanyakan ulang)

Hal-hal ini sudah diputuskan. Jangan tanya ulang atau ubah tanpa instruksi eksplisit:

- **Stack:** Python 3.12, FastAPI, HTMX, SQLite (aiosqlite), Pydantic, httpx, tenacity, structlog. Final.
- **LLM:** Hybrid. Ollama (`qwen2.5:3b/7b/14b`) untuk ringan, Claude (`claude-haiku-4-5-20251001`, `claude-sonnet-4-6`) untuk berat. Final.
- **Interface:** Web UI dengan SSE streaming. Final untuk research phase.
- **Fase:** Research/eksperimen. Single-user. Belum perlu auth, multi-tenant, atau scaling horizontal.
- **Nama model Claude:** sudah diverifikasi benar per Juni 2026. Jangan "perbaiki" ke versi lama.
- **Decay:** eksponensial, base 0.97, throttle 1 jam, archive di 0.3. Final.
- **Confidence threshold:** 4 dari 5. Final kecuali data menunjukkan perlu disesuaikan.

---

## 8. Saat kamu ragu

- **Spec dan CLAUDE.md bertentangan?** Spec menang untuk "apa", CLAUDE.md menang untuk "bagaimana". Jika benar-benar konflik, hentikan dan tanya manusia.
- **Spec tidak menjelaskan detail?** Pilih solusi paling sederhana yang konsisten dengan prinsip §1. Tinggalkan komentar `# ASUMSI: ...` agar bisa direview.
- **Butuh dependency baru?** Tanya dulu. Default-nya: jangan tambah dependency. Stack sudah final.
- **Tergoda menambah fitur?** Jangan. Selesaikan sprint dulu. Fitur di luar 4 inovasi + roadmap adalah scope creep.
- **Test sulit dibuat karena desain?** Itu sinyal desainnya perlu diperbaiki. Refactor agar testable, jangan skip test.

---

## 9. Format komunikasi dengan manusia

Saat melaporkan progres atau bertanya:
- **Ringkas.** Apa yang selesai, apa yang berikutnya, blocker (jika ada).
- **Tunjukkan bukti.** Output test, hasil `ruff`, query SQLite verifikasi.
- **Satu pertanyaan jika perlu klarifikasi**, bukan daftar panjang.
- Jangan minta konfirmasi untuk hal yang sudah jelas di spec/CLAUDE.md — kerjakan saja.

---

## 10. Checklist saat memulai di repository baru

Saat file ini dibawa ke repo baru, lakukan ini berurutan:

1. [ ] Baca CLAUDE.md (file ini) sampai habis.
2. [ ] Baca `openclawn-core-spec-v0.3.md` sampai habis, terutama §6 (schema), §7-18 (modul), §21 (roadmap).
3. [ ] Verifikasi environment: Python 3.12+, Docker, Ollama terpasang & jalan.
4. [ ] Buat struktur direktori sesuai spec §5.
5. [ ] Tulis `pyproject.toml` sesuai spec §4.
6. [ ] Tulis `migrations/001_initial.sql` sesuai spec §6.
7. [ ] Mulai Sprint 0: `infra/` → `llm_client.py` → `agent_loop.py` minimal → Web UI → audit dasar.
8. [ ] Setelah tiap komponen: test hijau + ruff bersih sebelum lanjut.

Jangan lompati langkah. Fondasi yang rapuh akan meruntuhkan semua di atasnya.

---

## Lampiran: Peta cepat spec → modul

| Mau kerjakan | Baca spec bagian | File yang dibuat |
|---|---|---|
| Config & DB | §7 | `infra/config.py`, `infra/database.py`, `infra/logging.py` |
| LLM + fallback | §8 | `core/llm_client.py` |
| Agent loop | §9 | `core/agent_loop.py` |
| Router | §10 | `core/router.py` |
| Audit (Inovasi 1) | §11 | `core/audit.py` |
| Skill decay (Inovasi 2) | §12 | `memory/skill_decay.py` |
| Crystallizer (Inovasi 3) | §13 | `core/crystallizer.py` |
| Contracts (Inovasi 4) | §14 | `roles/contracts.py`, `roles/registry.py` |
| Memory | §15 | `memory/layers.py`, `memory/search.py` |
| Tools + sandbox | §16 | `tools/*.py` |
| Security | §17 | `security/*.py` |
| Web UI | §18 | `web/main.py`, `web/templates/*` |
| Role config | §19 | `roles/{pm,qa,dev}/soul.toml` |
| Test | §20 | `tests/*.py` |

---

*CLAUDE.md v1.0 — selaras dengan openclawn-core-spec-v0.3.md. Update bersamaan jika spec berubah.*
