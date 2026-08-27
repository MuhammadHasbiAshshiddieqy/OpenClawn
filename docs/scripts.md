# `scripts/` — Tooling Pengembangan

Script utilitas untuk pengembangan dan demo. **Bukan bagian runtime** — tidak masuk package (lihat `pyproject.toml` `packages.find`). Setiap script menambah root proyek ke `sys.path` sendiri agar import absolut (`core.*`, `infra.*`) bekerja saat dijalankan dari mana pun.

> **Konteks:** Sprint 4 punya dua item yang ter-block menunggu **traffic nyata** (tuning threshold router) dan **bukti kebutuhan** (embedding routing). Keduanya blocker *epistemik* — tidak bisa di-unblock dengan data buatan. Script di sini **tidak** meng-unblock keputusan itu; mereka membuat sistem **siap** untuk tuning dan membuat `/metrics` bisa di-demo. Lihat [core.md §calibration](core.md) dan `core/calibration.py`.

---

## `scripts/seed_routing.py`

Mengisi tabel `routing_events` dengan baris **sintetis** untuk mendemo `/metrics` dan memvalidasi pipa kalibrasi end-to-end pada volume (ratusan baris) — bukan sekadar unit test.

> ⚠️ **Data yang dihasilkan ADALAH BUATAN.** Jangan dipakai untuk menyetel threshold router. Menyetel router dari data buatan = melingkar (memvalidasi asumsi dengan asumsi sendiri). Lihat CLAUDE.md §1.4, §8.

### Cara pakai

```bash
python scripts/seed_routing.py            # insert ~200 baris seed
python scripts/seed_routing.py --n 500    # jumlah kustom
python scripts/seed_routing.py --clear    # hapus semua baris seed
python scripts/seed_routing.py --db data/demo.db   # DB lain
python scripts/seed_routing.py --seed 7   # RNG seed berbeda (reproducible)
```

### Argumen

| Argumen | Default | Keterangan |
|---|---|---|
| `--n` | `200` | Jumlah baris seed yang di-insert |
| `--clear` | — | Hapus semua baris seed lalu keluar |
| `--db` | dari config/env | Path DB target |
| `--seed` | `42` | RNG seed untuk hasil reproducible |

### Cara kerja

- Semua baris diberi `session_id` berprefix `seed-` agar mudah dibedakan dari data nyata dan dihapus bersih via `--clear`.
- **`created_at` sengaja di-backdate** `MIN_RETENTION_DAYS + 5` hari (§ Prioritas 9.1 follow-up, `infra/retention.py`) — trigger retensi EU AI Act di `routing_events` menolak DELETE untuk baris < 180 hari, jadi tanpa ini `--clear` akan gagal `sqlite3.IntegrityError` untuk data seed yang baru diinsert. Bukan pengecualian di trigger (trigger tetap berlaku SAMA untuk semua baris, termasuk data uji) — solusinya di sisi script, bukan melubangi mekanisme kepatuhan.
- `PROFILES` mendefinisikan bobot kemunculan, correction rate target, dan biaya per label. Sengaja dirancang agar memunculkan **kedua** jenis rekomendasi `RoutingCalibrator`:
  - `complex` → correction rate tinggi (~28%) → **under_provisioned**
  - `critical` → correction rate rendah (~2%) + berbiaya → **over_provisioned**
- Query contoh netral (tanpa domain/locale spesifik, CLAUDE.md §1.5).
- Reuse `DatabaseManager` (tidak membuat koneksi sendiri) dan menjalankan migration idempoten dulu.

### Fungsi

| Fungsi | Keterangan |
|---|---|
| `seed(db, n, rng) → int` *(async)* | Insert `n` baris seed, return jumlah |
| `clear(db) → int` *(async)* | Hapus baris berprefix `seed-`, return jumlah dihapus |
| `_weighted_label(rng) → str` | Pilih label berdasarkan bobot di `PROFILES` |
| `_row_for_label(label, idx, rng) → tuple` | Bangun satu tuple parameter INSERT konsisten dengan profil |
| `_quick_report(db) → list[dict]` *(async)* | Ringkasan per label untuk output terminal |

### Verifikasi pipa (contoh)

Setelah seed, `RoutingCalibrator` harus menghasilkan rekomendasi:

```bash
python scripts/seed_routing.py --db /tmp/seed_test.db --n 200
# → complex   total=24  rate=25.0%   (under_provisioned)
# → critical  total=17  rate=0.0%    (over_provisioned)
```

---

## `scripts/route_sensitivity.py`

Simulasi keputusan router pada query sintetis di berbagai `threshold_shift`. Karena `SmartRouter.decide()` **deterministik** (murni fungsi dari teks, tanpa LLM/DB), kita bisa membangun **intuisi** "kalau threshold bergeser, query mana yang pindah tier" tanpa traffic nyata.

> Alat **bersiap**, bukan alat keputusan. Output menunjukkan arah & dampak pergeseran, tapi keputusan tuning tetap menunggu data audit nyata.

### Cara pakai

```bash
python scripts/route_sensitivity.py                 # role pm, shift -1..+1
python scripts/route_sensitivity.py --role dev
python scripts/route_sensitivity.py --shifts -2 -1 0 1 2
```

### Argumen

| Argumen | Default | Keterangan |
|---|---|---|
| `--role` | `pm` | Role soul yang dipakai (`pm`/`qa`/`dev`) — memengaruhi `prefer_local` & `upgrade_keywords` |
| `--shifts` | `-1 0 1` | Daftar `threshold_shift` yang disimulasi |

### Cara kerja

- Untuk tiap query: hitung skor router sekali (`_dimensions` → `_score`, + soul upgrade), lalu petakan ke label pada tiap `threshold_shift` via `_label`.
- `threshold_shift` dinaikkan = query bertahan di tier lebih rendah lebih lama — mekanisme yang sama yang dipakai `prefer_local` di [router.py](../core/router.py).
- Menampilkan tabel per-query + ringkasan berapa query yang berpindah tier relatif baseline (shift 0), plus legenda tier→model.

### Fungsi

| Fungsi | Keterangan |
|---|---|
| `run(role, shifts) → None` | Jalankan simulasi dan cetak tabel + ringkasan |
| `_label_at_shift(router, query, shift) → tuple[Complexity, int]` | Hitung label pada shift tertentu (setia pada `decide()`) |
| `_model_short(label) → str` | Format `model (provider)` untuk legenda |

### Contoh output (role pm)

```
query                                     score  shift-1  shift+0  shift+1
apa itu REST API?                             2  moderate   simple  trivial
rancang strategi migrasi database ...         7  critical critical  complex   ← kena soul "strategi" (+3)
```

> Catatan: `rancang strategi migrasi` mencapai score 7 karena keyword `strategi` ada di `upgrade_keywords` PM (soul bypass `prefer_local`). Ini contoh nyata interaksi soul ↔ router.

---

## `scripts/run_evals.py` — Eval Harness (TODO.md § Prioritas 8.2)

Jalankan kasus uji (`evals/<role>/*.yaml`) lewat `AgentLoop` **sungguhan** — butuh Ollama nyala. Beda dari `seed_routing.py`/`route_sensitivity.py` (deterministik, tanpa LLM), skrip ini justru SENGAJA memanggil model nyata untuk mendeteksi regresi kualitas jawaban yang tak terlihat dari test dengan LLM di-mock. Logika evaluasi murni ada di [`core/eval_harness.py`](../core/eval_harness.py) (diuji lewat pytest); skrip ini hanya menjembatani ke `AgentLoop` nyata.

> **Bukan CI gate** — CI tak punya akses Ollama. Alat dev/manual, dijalankan siapa pun yang punya Ollama lokal.

### Cara pakai

```bash
python scripts/run_evals.py --path evals/dev                        # semua kasus role dev
python scripts/run_evals.py --path evals                            # semua role
python scripts/run_evals.py --path evals/dev/basic.yaml             # satu file
python scripts/run_evals.py --path evals/dev --model ollama:qwen2.5:3b
python scripts/run_evals.py --path evals/dev --timeout 10           # timeout approval/question (detik)
```

### Argumen

| Argumen | Default | Keterangan |
|---|---|---|
| `--path` | `evals` | File atau direktori kasus uji |
| `--model` | *(kosong)* | Override `provider:model` (mis. `ollama:qwen2.5:3b`). Kosong → `SmartRouter` otomatis per role |
| `--timeout` | `5` | Timeout approval/question (detik) — pendek sengaja, lihat "Cara kerja" |

### Cara kerja

- Tiap kasus dijalankan di DB (`:memory:`) + workspace **temporer terpisah** — tak ada state bocor antar kasus.
- `AgentConfig(autopilot=True)` — tool butuh-approval DIANTRI sebagai proposal (tak dieksekusi), bukan menunggu manusia yang tak akan pernah ada. `Turn.tool_calls` tetap mencatat NIAT model memanggilnya (dicek `_execute_tool` SEBELUM approval diputuskan) — rubrik `tool_called`/`tool_not_called` mengukur **pilihan** model, bukan hasil eksekusi tool.
- **Ditemukan lewat run nyata (bukan diasumsikan):** `AgentLoop.run()` menjadwalkan `_post_turn` sebagai background task fire-and-forget (`asyncio.create_task`) yang MASIH JALAN setelah generator `run()` habis. DB berumur-pendek skrip ini (beda dari server produksi yang koneksinya tetap hidup) menutup diri terlalu cepat tanpa ini, menyebabkan `_post_turn` gagal `"Cannot operate on a closed database"` di tengah jalan. Diperbaiki: tangkap task baru yang muncul selama `run()`, tunggu sebelum `db.close()`.
- **Timeout tunggu task tersebut 60 detik (bukan 10)**, DAN mengecek eksplisit apakah masih `pending` setelahnya — TODO.md § Prioritas 8.4 mendokumentasikan investigasi penuh: timeout 10 detik yang lebih pendek TERBUKTI tidak cukup untuk `_post_turn`'s SENDIRI cascade fallback (`_generate_session_title` bisa mencoba 4 model), menyebabkan `db.close()` jalan sementara task itu masih berjalan DAN kasus BERIKUTNYA sudah mulai — muncul sebagai `"no such table"` yang membingungkan (bukan "closed database" yang jelas) di kasus SETELAHNYA. Kalau timeout 60 detik pun terlampaui, DB SENGAJA TIDAK ditutup (dibiarkan bocor sampai proses keluar) — jauh lebih aman daripada mengulang bug ini.
- Skor via `core/eval_harness.py::evaluate_rubric()` — rubrik deterministik, BUKAN LLM-judge.
- Exit code `0` = semua lolos, `1` = ada yang gagal.

### Format kasus uji (`evals/<role>/*.yaml`)

```yaml
- name: reads-file-before-answering
  input: "Baca isi file notes.txt, lalu sebutkan angka di dalamnya."
  setup_files:
    notes.txt: "Catatan penting: jawabannya adalah 42."
  expect:
    tool_called: ["file_read"]
    contains: ["42"]
```

`role` kasus = nama folder induk file (`evals/dev/x.yaml` → role `dev`), bukan field di YAML. Satu file = satu list kasus (boleh banyak).

### Verifikasi nyata (2026-08-03)

Dijalankan langsung terhadap Ollama lokal (`qwen2.5:3b`, via `uv run --python 3.12` karena mesin dev hanya punya Python 3.9): mekanisme terbukti bekerja end-to-end (setup workspace → jalankan agent nyata → skor rubrik → laporan). Kasus `does_not_call_code_run_for_simple_arithmetic` **PASS**; kasus `reads-file-before-answering` **FAIL** — model 3B sempat memanggil tool dengan nama kosong (`tool_loop_detected` — mekanisme deteksi loop yang sudah ada bekerja benar) sebelum akhirnya menjawab tanpa membaca file. Ini temuan kualitas MODEL yang sah (model kecil kadang gagal format tool-call), bukan bug harness — persis kelas masalah yang harness ini dibuat untuk menangkap.

Run nyata ini JUGA menyingkap satu bug di skrip ini sendiri (`"no such table: memory_l1"` di kasus berikutnya) — diinvestigasi & diperbaiki penuh (§ TODO.md Prioritas 8.4): timeout tunggu `_post_turn` yang tadinya 10 detik terlalu pendek untuk cascade fallback title-generation, sekarang 60 detik + pengecekan eksplisit. Diverifikasi ulang dengan reproduksi yang sama persis — bersih.

---

## Catatan untuk Maintainer

- Kedua script aman dijalankan berkali-kali (idempoten untuk migration; seed pakai prefix khusus).
- Untuk demo `/metrics` cepat: `python scripts/seed_routing.py` lalu buka `http://localhost:8000/metrics`.
- Untuk membersihkan: `python scripts/seed_routing.py --clear`.
- **Jangan** menambah logika yang menyetel threshold router otomatis dari output script ini — itu melanggar CLAUDE.md §1.4 dan §8.
