"""Sensitivity analysis routing — simulasi keputusan router pada query sintetis.

SmartRouter.decide() DETERMINISTIK: murni fungsi dari teks (tanpa LLM, tanpa DB).
Sifat itu kita manfaatkan untuk membangun INTUISI "kalau threshold bergeser,
query mana yang pindah tier" — tanpa perlu traffic nyata.

Ini alat BERSIAP, bukan alat keputusan. Output di sini menunjukkan arah & dampak
sebuah pergeseran threshold, tapi keputusan menyetel router tetap menunggu data
audit nyata (CLAUDE.md §1.4, §8). Lihat juga core/calibration.py.

Cara kerja: untuk tiap query contoh, hitung skor router sekali, lalu petakan ke
label pada beberapa nilai threshold_shift (-1, 0, +1). threshold_shift adalah
mekanisme yang sama yang dipakai prefer_local di SmartRouter._label — menaikkannya
membuat query "bertahan" di tier lebih rendah (lebih lama di Ollama) lebih lama.

Pakai:
    python scripts/route_sensitivity.py                 # role pm, shift -1..+1
    python scripts/route_sensitivity.py --role dev
    python scripts/route_sensitivity.py --shifts -2 -1 0 1 2
"""

import argparse
import sys
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (core.*, infra.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.router import Complexity, SmartRouter  # noqa: E402

# Query contoh netral (tanpa domain/locale spesifik, CLAUDE.md §1.5).
# Dipilih agar tersebar di sekitar batas antar-tier supaya pergeseran terlihat.
SAMPLE_QUERIES: list[str] = [
    "hi",
    "thanks, lanjut",
    "apa itu REST API?",
    "jelaskan singkat cara kerja git rebase",
    "tolong review fungsi ini untuk edge case",
    "buat query SQL join tiga tabel dengan filter",
    "refactor modul ini agar lebih mudah ditest",
    "debug race condition pada antrian async yang intermittent",
    "rancang strategi migrasi database tanpa downtime",
    "analisis bottleneck performa lalu bandingkan dua pendekatan deploy",
    "evaluasi arsitektur multi-region untuk disaster recovery, urgent",
]


def _label_at_shift(router: SmartRouter, query: str, shift: int) -> tuple[Complexity, int]:
    """Hitung label pada threshold_shift tertentu, menggunakan jalur skor router asli.

    Catatan: kita panggil _dimensions/_score/_label (API internal router) secara
    sengaja — tujuannya men-simulasi _label di berbagai shift tanpa mengubah router.
    Skor soul upgrade tetap diterapkan agar simulasi setia pada decide() sebenarnya.
    """
    dims = router._dimensions([], query)
    soul_hit = any(k.lower() in query.lower() for k in router.soul_upgrade_kw)
    dims["soul_upgrade_hit"] = int(soul_hit)
    score = router._score(dims)
    if soul_hit:
        score += 3
    return router._label(score, shift), score


def _model_short(label: Complexity) -> str:
    model, provider, _ = SmartRouter.MODELS[label]
    return f"{model} ({provider})"


def run(role: str, shifts: list[int]) -> None:
    router = SmartRouter(role=role)
    prefer = router.prefer_local
    print(f"Role: {role}   prefer_local={prefer}   upgrade_keywords={router.soul_upgrade_kw}")
    print(
        "threshold_shift dinaikkan = query bertahan di tier lebih rendah lebih lama "
        "(efek prefer_local).\n"
    )

    header = f"{'query':<52} {'score':>5}  " + "  ".join(f"shift{s:+d}" for s in shifts)
    print(header)
    print("-" * len(header))

    shift_changes = {s: 0 for s in shifts}
    for query in SAMPLE_QUERIES:
        labels = {}
        score_val = None
        for s in shifts:
            label, score_val = _label_at_shift(router, query, s)
            labels[s] = label
        cells = "  ".join(f"{labels[s].value:>6}" for s in shifts)
        q_disp = (query[:49] + "...") if len(query) > 52 else query
        print(f"{q_disp:<52} {score_val:>5}  {cells}")

        # Hitung berapa query yang labelnya berubah saat shift bergeser dari baseline 0.
        if 0 in labels:
            for s in shifts:
                if s != 0 and labels[s] != labels[0]:
                    shift_changes[s] += 1

    print("\nRingkasan pergeseran relatif terhadap shift 0 (baseline):")
    for s in shifts:
        if s == 0:
            continue
        arah = "lebih murah/lokal" if s > 0 else "lebih agresif ke cloud"
        print(
            f"  shift {s:+d}: {shift_changes[s]} dari {len(SAMPLE_QUERIES)} query berubah tier ({arah})"
        )

    print("\nLegenda tier → model:")
    for label in Complexity:
        print(f"  {label.value:<9} → {_model_short(label)}")
    print("\nCATATAN: ini simulasi untuk intuisi. Keputusan tuning menunggu data audit nyata.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="pm", choices=["pm", "qa", "dev"], help="Role soul")
    parser.add_argument(
        "--shifts",
        type=int,
        nargs="+",
        default=[-1, 0, 1],
        help="Daftar threshold_shift yang disimulasi (default: -1 0 1)",
    )
    args = parser.parse_args()
    run(args.role, sorted(set(args.shifts)))


if __name__ == "__main__":
    main()
