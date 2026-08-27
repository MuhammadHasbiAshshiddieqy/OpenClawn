"""Anchor rantai audit ke file lokal terpisah dari database (TODO.md § Prioritas
9.1 follow-up).

Gap: `AuditChain.verify()` (core/audit_chain.py) TIDAK menangkap truncation
(entry terakhir dihapus) atau penulisan-ulang seluruh rantai — keduanya
menghasilkan rantai yang secara internal tetap konsisten. Skrip ini
membungkus `core/audit_anchor.py` untuk dipakai lewat cron/systemd timer,
sama pola `scripts/backup_db.py`.

Pakai:
    python scripts/anchor_audit_chain.py              # tulis anchor baru bila ada aktivitas baru
    python scripts/anchor_audit_chain.py --verify      # cek semua anchor tersimpan vs tabel sekarang
    python scripts/anchor_audit_chain.py --db data/openclawn.db --anchor-path data/audit_anchors.jsonl

Cron tiap jam (gabung dengan cron backup yang sudah ada):
    0 * * * * cd /path/to/openclawn && .venv/bin/python scripts/anchor_audit_chain.py >> data/anchor.log 2>&1

PENTING — baca sebelum menganggap ini "selesai": file anchor (default
data/audit_anchors.jsonl) BARU jadi anchor yang independen dari mesin yang
dijaga kalau disalin OFF-HOST secara berkala (rsync, git, backup terpisah).
Disimpan di disk yang sama dengan database TIDAK melindungi dari penyerang
yang punya akses tulis ke keduanya — lihat docstring core/audit_anchor.py
untuk batas jaminan lengkapnya. Skrip ini TIDAK mengatur ke mana file
disalin, sama seperti backup_db.py tidak mengatur ke mana backup disalin.

systemd timer — contoh unit:

    # /etc/systemd/system/openclawn-anchor.service
    [Unit]
    Description=OpenCLAWN audit chain anchor

    [Service]
    Type=oneshot
    WorkingDirectory=/path/to/openclawn
    ExecStart=/path/to/openclawn/.venv/bin/python scripts/anchor_audit_chain.py

    # /etc/systemd/system/openclawn-anchor.timer
    [Unit]
    Description=Anchor OpenCLAWN audit chain hourly

    [Timer]
    OnCalendar=hourly
    Persistent=true

    [Install]
    WantedBy=timers.target
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (infra.*, core.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config import CONFIG  # noqa: E402
from infra.database import DatabaseManager  # noqa: E402
from core.audit_anchor import verify_against_anchors, write_anchor  # noqa: E402

DEFAULT_DB = "data/openclawn.db"
DEFAULT_ANCHOR_PATH = "data/audit_anchors.jsonl"


async def _run(db_path: str, anchor_path: str, verify: bool) -> int:
    db = DatabaseManager(replace(CONFIG, db_path=db_path))
    try:
        if verify:
            result = await verify_against_anchors(db, anchor_path)
            if result["ok"]:
                print(f"OK — {result['anchors_checked']} anchor cocok dengan tabel saat ini.")
                return 0
            print(f"GAGAL — {result['reason']}", file=sys.stderr)
            for f in result["failed"]:
                print(
                    f"  id={f['id']} anchored_at={f['anchored_at']}: {f['problem']}",
                    file=sys.stderr,
                )
            return 1

        entry = await write_anchor(db, anchor_path)
        if entry is None:
            print("Tidak ada aktivitas baru sejak anchor terakhir — tidak menulis anchor baru.")
        else:
            print(
                f"Anchor ditulis: id={entry['id']} hash={entry['record_hash'][:16]}... "
                f"ke {anchor_path}"
            )
        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Path sumber DB (default: {DEFAULT_DB})")
    parser.add_argument(
        "--anchor-path",
        default=DEFAULT_ANCHOR_PATH,
        help=f"Path file anchor (default: {DEFAULT_ANCHOR_PATH})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verifikasi anchor tersimpan vs tabel sekarang, jangan tulis anchor baru",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.db, args.anchor_path, args.verify)))


if __name__ == "__main__":
    main()
