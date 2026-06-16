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

### `CONTRACT_REGISTRY`

Dict mapping role → contract class:
```python
CONTRACT_REGISTRY = {
    "pm": PMOutput,
    "qa": QAOutput,
    "dev": DevOutput,
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
4. Validasi output dengan `_validate()`
5. Simpan ke tabel `role_handoffs` (selalu simpan, valid atau tidak — untuk debugging)

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

## `roles/{pm,qa,dev}/soul.toml`

File konfigurasi kepribadian tiap role. Dibaca oleh `SmartRouter` dan `AgentLoop`.

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

| Role | `prefer_local` | Upgrade Keywords | Tool yang Diizinkan |
|---|---|---|---|
| **pm** | `true` | arsitektur, strategi, roadmap, OKR, architecture, strategy | file_read, file_write, web_fetch, ask_user |
| **qa** | `false` | security, performance, race condition, injection, vulnerability | file_read, file_write, code_run, ask_user |
| **dev** | `false` | arsitektur, refactor, migrate, database, deploy, architecture | file_read, file_write, code_run, web_fetch |

**`prefer_local = true`** pada PM artinya: PM lebih suka tetap di Ollama untuk query biasa, naik ke Claude hanya jika kata kunci upgrade cocok atau skor tinggi. QA dan Dev tidak prefer local — lebih agresif naik ke cloud jika perlu.

---

## Cara Menambah Role Baru

1. Buat folder `roles/namaRole/`
2. Tulis `soul.toml` dengan format di atas
3. Tambah contract class di `contracts.py`
4. Daftarkan di `CONTRACT_REGISTRY`
5. Tulis test validasi contract
