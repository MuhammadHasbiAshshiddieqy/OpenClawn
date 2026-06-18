# `roles/` — Sistem Multi-Role dan Kontrak Handoff

Inovasi 4: handoff antar role menggunakan Pydantic contract yang tervalidasi. Output tidak valid disimpan dengan `validation_ok=0` untuk debugging — tidak pernah crash.

---

## `roles/contracts.py`

Mendefinisikan contract (tipe output) untuk setiap role. Semua contract adalah Pydantic `BaseModel`.

### `PMOutput`

Contract output untuk role **Product Manager**.

| Field | Tipe | Default | Keterangan |
|---|---|---|---|
| `summary` | `str` | wajib | Ringkasan permintaan/keputusan |
| `user_stories` | `list[str]` | `[]` | Daftar user story |
| `acceptance_criteria` | `list[str]` | `[]` | Kriteria penerimaan |
| `priority` | `str` | `"medium"` | Prioritas: `"low"`, `"medium"`, atau `"high"` |
| `open_questions` | `list[str]` | `[]` | Pertanyaan terbuka yang belum terjawab |

### `QAOutput`

Contract output untuk role **QA Engineer**.

| Field | Tipe | Default | Keterangan |
|---|---|---|---|
| `test_cases` | `list[str]` | `[]` | Daftar test case |
| `coverage_gaps` | `list[str]` | `[]` | Area yang belum tercover test |
| `severity_matrix` | `dict[str, str]` | `{}` | Map komponen → severity level |
| `pass_criteria` | `list[str]` | `[]` | Kriteria pass/fail test suite |

### `DevOutput`

Contract output untuk role **Software Developer**.

| Field | Tipe | Default | Keterangan |
|---|---|---|---|
| `approach` | `str` | wajib | Deskripsi pendekatan implementasi |
| `files_changed` | `list[str]` | `[]` | Daftar file yang dimodifikasi |
| `risks` | `list[str]` | `[]` | Risiko yang teridentifikasi |
| `needs_review` | `bool` | `True` | Apakah butuh code review |

### `DataOutput`

Contract output untuk role **Data** (analisis, eksplorasi, statistik, modeling dasar). Sengaja kaya: analisis tanpa metodologi & keterbatasan eksplisit mudah menyesatkan.

| Field | Tipe | Default | Keterangan |
|---|---|---|---|
| `summary` | `str` | wajib | Ringkasan temuan utama |
| `findings` | `list[str]` | `[]` | Temuan terperinci |
| `methodology` | `str` | `""` | Bagaimana angka dihitung (sumber, langkah) |
| `metrics` | `dict[str, str]` | `{}` | Map metrik → nilai |
| `caveats` | `list[str]` | `[]` | Keterbatasan, bias, asumsi |
| `recommendations` | `list[str]` | `[]` | Rekomendasi tindak lanjut |
| `confidence` | `str` | `"medium"` | `"low"`, `"medium"`, atau `"high"` |

### `SecurityOutput`

Contract output untuk role **Security & Privacy** (advisory). Lapisan saran governance — **bukan** jaminan keamanan (pertahanan utama tetap isolasi container & Vault, lihat CLAUDE.md §1 & §17).

| Field | Tipe | Default | Keterangan |
|---|---|---|---|
| `summary` | `str` | wajib | Ringkasan postur risiko |
| `pii_detected` | `bool` | `False` | Apakah data pribadi teridentifikasi dalam ruang lingkup |
| `findings` | `list[str]` | `[]` | Temuan risiko terperinci |
| `severity_matrix` | `dict[str, str]` | `{}` | Map temuan → severity |
| `mitigations` | `list[str]` | `[]` | Mitigasi yang dapat ditindak |
| `compliance_notes` | `list[str]` | `[]` | Catatan kepatuhan (mis. retensi, minimisasi) |
| `risk_level` | `str` | `"medium"` | `"low"`, `"medium"`, `"high"`, atau `"critical"` |

### `CONTRACT_REGISTRY`

Dict mapping role → contract class:
```python
CONTRACT_REGISTRY = {
    "pm": PMOutput,
    "qa": QAOutput,
    "dev": DevOutput,
    "data": DataOutput,
    "security": SecurityOutput,
}
```

---

## `roles/registry.py`

Mengatur handoff antar role dengan validasi contract.

### Kelas: `RoleNegotiator`

**`__init__(db)`**

**`handoff(session_id, from_role, to_role, task_input, agent_factory) → dict`** *(async)*  
Alur handoff lengkap:

1. Lookup contract dari `CONTRACT_REGISTRY[to_role]`
2. Instansiasi sub-agent via `agent_factory(to_role)`
3. Prompt sub-agent dengan task + schema JSON contract
4. Kumpulkan teks dari event `type=="token"` (sub-agent `run()` yield `AgentEvent`, bukan str)
5. Validasi output dengan `parse_contract()`
6. Simpan ke tabel `role_handoffs` (selalu simpan, valid atau tidak — untuk debugging)

> **Helper modul-level `parse_contract(raw, contract_cls) → tuple[dict, bool]`** — parse JSON (toleran pembungkus ```json) → instance contract; gagal → `({"raw","error"}, False)`. Dipakai ulang oleh `RoleNegotiator` dan `ConversationOrchestrator` (`core/conversation.py`), yang juga menulis `role_handoffs` untuk pola Pipeline (degrade graceful: validasi gagal → teruskan teks mentah, percakapan tetap lanjut).

Return:
```python
{
    "from": "pm",
    "to": "qa",
    "output": {...},  # dict dari Pydantic model atau {"raw": ..., "error": ...}
    "valid": True     # atau False jika validasi gagal
}
```

**`_validate(raw, contract_cls) → tuple[dict, bool]`** *(private)*  
Parse JSON dari output LLM, validasi dengan Pydantic:
- Sukses → `(model.model_dump(), True)`
- Gagal (JSON error atau validation error) → `({"raw": ..., "error": ...}, False)`

Tidak pernah raise exception — selalu return tuple.

---

## `roles/{pm,qa,dev,data,security}/soul.toml`

File konfigurasi kepribadian tiap role. Dibaca oleh `SmartRouter` dan `AgentLoop`.

Semua soul kini menyertakan blok **THINK BEFORE YOU ACT** (PLAN → ACT → CRITIQUE): agent merencanakan langkah & asumsi sebelum memakai tool, lalu mengkritik jawabannya sendiri sebelum final (menyelaraskan dengan Inovasi #3 confidence-gated crystallization). Murni prompt — tanpa dependency baru.

### Format

```toml
[meta]
role = "pm"
name = "PM Agent"

[system_prompt]
content = """
Kamu adalah agent Product Manager.
...
"""

[tools]
allowed = ["file_read", "web_fetch", ...]

[routing]
prefer_local = true
upgrade_keywords = ["arsitektur", "strategi", ...]

[contract]
output_type = "PMOutput"
```

### Perbedaan antar role

| Role | `prefer_local` | Fokus | Akses tool |
|---|---|---|---|
| **pm** | `true` | Breakdown, prioritas, acceptance criteria | baca + tulis file, web_search/fetch (tanpa eksekusi) |
| **qa** | `false` | Review, test case, edge case | baca + tulis, shell_run/code_run (sandboxed) |
| **dev** | `false` | Implementasi, fix, refactor | set penuh: baca/tulis/edit/patch, shell_run/code_run, http_request |
| **data** | `false` | Analisis, eksplorasi, statistik, insight, modeling dasar | baca + db_query (SELECT) + code_run (hitung statistik/modeling di sandbox); **tanpa tulis file** |
| **security** | `true` | Audit keamanan & privasi (advisory) | **read-only mutlak**: glob/grep/list_dir/file_read/pdf_read/db_query(SELECT)/json_query/memory_search; tanpa tulis/eksekusi/network |

**`prefer_local = true`** (PM, security): cenderung tetap di Ollama untuk query biasa, naik ke cloud hanya jika kata kunci upgrade cocok atau skor tinggi. Untuk **security**, ini juga pilihan privasi — data sensitif lebih baik tidak keluar box bila tidak perlu. QA/Dev/Data tidak prefer local — lebih agresif naik ke cloud untuk tugas berat.

**Role `security` read-only by design** (keputusan owner, selaras CLAUDE.md §17): ia *menyarankan* mitigasi, tidak menerapkannya. Bila perlu perubahan, ia menyerahkan ke Dev. Ini ditegakkan oleh test `test_security_role_is_read_only` — menambah tool tulis/eksekusi ke soul security akan menggagalkan test.

---

## Cara Menambah Role Baru

1. Buat folder `roles/namaRole/` + tulis `soul.toml` (format di atas, termasuk blok PLAN → ACT → CRITIQUE).
2. Tambah contract class di `contracts.py` dan daftarkan di `CONTRACT_REGISTRY` (key = nama folder).
3. Web UI **otomatis menemukan** role via `available_roles()` di `web/main.py` (scan `roles/*/soul.toml`). Untuk mengatur urutan tampil & label, tambahkan ke `_ROLE_ORDER` dan `ROLES_META` di `web/main.py`, dan (opsional) label chip pendek di `ROLE_LABEL`/`chip_labels` pada `web/templates/index.html`. Untuk warna bubble, tambahkan aturan `.msg.assistant.role-<nama>` + `.role-chip.active[data-role="<nama>"]` di `web/static/style.css`.
4. Tulis test: validasi contract + (otomatis tercakup) `test_all_souls_loadable_and_well_formed` & `test_soul_output_type_matches_registry` akan memeriksa konsistensi soul ↔ contract.
