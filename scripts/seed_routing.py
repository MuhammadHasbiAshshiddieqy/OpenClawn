"""Seed routing_events sintetis — untuk demo /metrics dan validasi pipa kalibrasi.

PERINGATAN PENTING (baca CLAUDE.md §1.4, §8 dan core/calibration.py header):
    Data yang dihasilkan script ini ADALAH BUATAN. Boleh dipakai untuk:
      - mendemo dashboard /metrics tanpa menunggu traffic nyata,
      - memvalidasi bahwa calibration_report() → RoutingCalibrator.summary()
        bekerja benar pada volume (ratusan baris), bukan sekadar unit test.
    TIDAK BOLEH dipakai untuk:
      - mengambil keputusan tuning threshold router.
        Threshold "benar" hanya bisa diketahui dari distribusi query nyata +
        sinyal koreksi user nyata. Menyetel router dari data buatan = melingkar.

Semua baris seed diberi session_id berprefix `SEED_PREFIX` agar mudah
dibedakan dari data nyata dan bisa dihapus bersih (`--clear`).

Pakai:
    python scripts/seed_routing.py            # insert ~200 baris seed
    python scripts/seed_routing.py --n 500    # jumlah kustom
    python scripts/seed_routing.py --clear    # hapus semua baris seed
    python scripts/seed_routing.py --db data/demo.db   # DB lain
"""

import argparse
import asyncio
import random
import sys
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (core.*, infra.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import AppConfig  # noqa: E402
from infra.database import DatabaseManager  # noqa: E402

SEED_PREFIX = "seed-"

# Profil sintetis per label: (bobot kemunculan, correction_rate target, biaya/turn).
# Angka ini SENGAJA dibuat untuk menampilkan beragam kondisi di /metrics —
# bukan klaim tentang traffic nyata. Catatan: 'complex' sengaja diberi
# correction_rate tinggi (under-provisioned) dan 'critical' rendah dengan biaya
# (over-provisioned) agar RoutingCalibrator memunculkan kedua jenis rekomendasi.
PROFILES: dict[str, dict] = {
    "trivial": {"weight": 30, "correction_rate": 0.02, "cost": 0.0, "provider": "ollama"},
    "simple": {"weight": 25, "correction_rate": 0.05, "cost": 0.0, "provider": "ollama"},
    "moderate": {"weight": 20, "correction_rate": 0.10, "cost": 0.0, "provider": "ollama"},
    "complex": {"weight": 15, "correction_rate": 0.28, "cost": 0.0012, "provider": "anthropic"},
    "critical": {"weight": 10, "correction_rate": 0.02, "cost": 0.0035, "provider": "anthropic"},
}

MODEL_FOR_LABEL = {
    "trivial": "gemma4:e2b",
    "simple": "gemma4:e4b",
    "moderate": "gemma4:12b",
    "complex": "claude-haiku-4-5-20251001",
    "critical": "claude-sonnet-4-6",
}

# Query contoh per label — netral, tanpa domain/locale spesifik (CLAUDE.md §1.5).
SAMPLE_QUERIES: dict[str, list[str]] = {
    "trivial": ["hi", "thanks", "ok lanjut", "ya", "halo"],
    "simple": ["apa itu REST?", "jelaskan singkat git rebase", "format tanggal ISO"],
    "moderate": [
        "buat query join 3 tabel",
        "review fungsi ini untuk edge case",
        "refactor loop jadi lebih bersih",
    ],
    "complex": [
        "debug race condition di antrian async",
        "rancang skema migrasi tanpa downtime",
        "analisis bottleneck performa endpoint",
    ],
    "critical": [
        "evaluasi arsitektur multi-region untuk strategi DR",
        "audit keamanan jalur autentikasi end-to-end",
    ],
}

ROLES = ["pm", "qa", "dev"]


def _weighted_label(rng: random.Random) -> str:
    labels = list(PROFILES)
    weights = [PROFILES[label]["weight"] for label in labels]
    return rng.choices(labels, weights=weights, k=1)[0]


def _row_for_label(label: str, idx: int, rng: random.Random) -> tuple:
    """Bangun satu tuple parameter INSERT yang konsisten dengan profil label."""
    prof = PROFILES[label]
    had_correction = 1 if rng.random() < prof["correction_rate"] else 0
    query = rng.choice(SAMPLE_QUERIES[label])
    tokens_in = rng.randint(20, 400)
    tokens_out = rng.randint(20, 800)
    # Skor numerik kasar yang konsisten dengan urutan kompleksitas (bukan dari router asli).
    score = {"trivial": 0, "simple": 2, "moderate": 4, "complex": 6, "critical": 8}[label]
    return (
        f"{SEED_PREFIX}{idx % 40}",  # session_id (beberapa turn berbagi sesi)
        rng.choice(ROLES),
        query,
        int(len(query.split()) * 1.3),  # dim_query_tokens
        1 if label in ("complex", "critical", "moderate") else 0,  # dim_has_tech_kw
        1 if label in ("complex", "critical") else 0,  # dim_needs_multistep
        rng.randint(0, 15),  # dim_history_len
        "dev",  # dim_role
        0,  # dim_has_urgency
        1,  # dim_needs_stream
        rng.randint(0, 1),  # dim_is_continuation
        0,  # dim_soul_upgrade_hit
        score,  # complexity_score
        label,  # complexity_label
        MODEL_FOR_LABEL[label],  # model_chosen
        prof["provider"],  # provider
        f"[seed] {label}",  # routing_reason
        0,  # fallback_used
        tokens_in,
        tokens_out,
        round(prof["cost"] * (tokens_in + tokens_out) / 1000, 6),  # cost_usd
        rng.randint(80, 4000),  # latency_ms
        had_correction,
        "salah, bukan itu" if had_correction else None,  # correction_detail
    )


INSERT_SQL = """
INSERT INTO routing_events (
    session_id, role, query_text,
    dim_query_tokens, dim_has_tech_kw, dim_needs_multistep,
    dim_history_len, dim_role, dim_has_urgency,
    dim_needs_stream, dim_is_continuation, dim_soul_upgrade_hit,
    complexity_score, complexity_label,
    model_chosen, provider, routing_reason, fallback_used,
    tokens_in, tokens_out, cost_usd, latency_ms,
    had_correction, correction_detail
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


async def seed(db: DatabaseManager, n: int, rng: random.Random) -> int:
    for idx in range(n):
        label = _weighted_label(rng)
        await db.execute(INSERT_SQL, _row_for_label(label, idx, rng))
    return n


async def clear(db: DatabaseManager) -> int:
    cursor = await db.execute(
        "DELETE FROM routing_events WHERE session_id LIKE ?", (f"{SEED_PREFIX}%",)
    )
    return cursor.rowcount


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="Jumlah baris seed (default 200)")
    parser.add_argument("--clear", action="store_true", help="Hapus semua baris seed lalu keluar")
    parser.add_argument("--db", default=None, help="Path DB (default dari config)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed untuk hasil reproducible")
    args = parser.parse_args()

    cfg = AppConfig(db_path=args.db) if args.db else AppConfig.from_env()
    db = DatabaseManager(cfg)
    # Pastikan skema ada (idempoten — CREATE TABLE IF NOT EXISTS).
    await db.run_migration("migrations/001_initial.sql")

    try:
        if args.clear:
            removed = await clear(db)
            print(f"Dihapus {removed} baris seed (session_id LIKE '{SEED_PREFIX}%').")
            return

        rng = random.Random(args.seed)
        inserted = await seed(db, args.n, rng)
        report = await _quick_report(db)
        print(f"Insert {inserted} baris seed ke {cfg.db_path}.")
        print("Ringkas per label (semua sumber, termasuk data nyata bila ada):")
        for row in report:
            print(
                f"  {row['complexity_label']:<9} total={row['total']:<4} "
                f"corrections={row['corrections'] or 0:<4} rate={row['correction_rate']}%"
            )
        print("\nBuka /metrics untuk melihat rekomendasi RoutingCalibrator.")
        print("CATATAN: data ini BUATAN — jangan dipakai menyetel threshold router.")
    finally:
        await db.close()


async def _quick_report(db: DatabaseManager) -> list[dict]:
    return await db.fetchall(
        """SELECT complexity_label, COUNT(*) as total, SUM(had_correction) as corrections,
                  ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate
           FROM routing_events GROUP BY complexity_label ORDER BY correction_rate DESC"""
    )


if __name__ == "__main__":
    asyncio.run(main())
