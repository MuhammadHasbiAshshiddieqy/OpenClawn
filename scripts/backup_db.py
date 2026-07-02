"""Backup/restore data/openclawn.db (§ production-readiness, TODO.md § Prioritas 1.5).

Gap dari PRODUCTION-READINESS.md: tidak ada mekanisme backup otomatis. Skrip ini
membungkus `infra/backup.py` (SQLite Online Backup API — aman dipanggil selagi
server hidup) untuk dipakai lewat cron/systemd timer.

Pakai:
    python scripts/backup_db.py                       # backup sekali, default paths
    python scripts/backup_db.py --db data/openclawn.db --out data/backups
    python scripts/backup_db.py --keep 14              # backup + retensi 14 file terbaru
    python scripts/backup_db.py --list                 # tampilkan backup yang ada

Cron harian jam 03:00, simpan 14 hari terakhir:
    0 3 * * * cd /path/to/openclawn && .venv/bin/python scripts/backup_db.py --keep 14 >> data/backup.log 2>&1

systemd timer — lihat komentar di akhir file ini untuk unit contoh.
"""

import argparse
import sys
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (infra.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.backup import backup_database, list_backups, prune_old_backups  # noqa: E402

DEFAULT_DB = "data/openclawn.db"
DEFAULT_BACKUP_DIR = "data/backups"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Path sumber DB (default: {DEFAULT_DB})")
    parser.add_argument(
        "--out",
        default=DEFAULT_BACKUP_DIR,
        help=f"Direktori tujuan backup (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="Simpan hanya N backup terbaru, hapus sisanya (default: tidak prune)",
    )
    parser.add_argument(
        "--list", action="store_true", help="Tampilkan backup yang ada, jangan buat baru"
    )
    args = parser.parse_args()

    if args.list:
        found = list_backups(args.out)
        if not found:
            print(f"Tidak ada backup di {args.out}")
        for path in found:
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"{path.name}  ({size_mb:.2f} MB)")
        return

    try:
        dest = backup_database(args.db, args.out)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"Backup dibuat: {dest} ({size_mb:.2f} MB)")

    if args.keep is not None:
        removed = prune_old_backups(args.out, keep=args.keep)
        if removed:
            print(f"Dihapus {len(removed)} backup lama (retensi {args.keep} terbaru):")
            for path in removed:
                print(f"  - {path.name}")


if __name__ == "__main__":
    main()

# --- Contoh systemd timer (self-host VPS, alternatif cron) ---
#
# /etc/systemd/system/openclawn-backup.service:
#   [Unit]
#   Description=OpenCLAWN database backup
#   [Service]
#   Type=oneshot
#   WorkingDirectory=/path/to/openclawn
#   ExecStart=/path/to/openclawn/.venv/bin/python scripts/backup_db.py --keep 14
#
# /etc/systemd/system/openclawn-backup.timer:
#   [Unit]
#   Description=Daily OpenCLAWN database backup
#   [Timer]
#   OnCalendar=daily
#   Persistent=true
#   [Install]
#   WantedBy=timers.target
#
# Aktifkan: systemctl enable --now openclawn-backup.timer
#
# --- Restore ---
# Backup adalah file SQLite utuh (bukan diff) — restore = ganti file:
#   systemctl stop openclawn   # atau hentikan proses server
#   cp data/backups/openclawn_20260702T030000.db data/openclawn.db
#   systemctl start openclawn
