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

## Catatan untuk Maintainer

- Kedua script aman dijalankan berkali-kali (idempoten untuk migration; seed pakai prefix khusus).
- Untuk demo `/metrics` cepat: `python scripts/seed_routing.py` lalu buka `http://localhost:8000/metrics`.
- Untuk membersihkan: `python scripts/seed_routing.py --clear`.
- **Jangan** menambah logika yang menyetel threshold router otomatis dari output script ini — itu melanggar CLAUDE.md §1.4 dan §8.
