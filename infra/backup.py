"""Backup/restore SQLite (§ production-readiness, TODO.md § Prioritas 1.5).

Gap yang tercatat di PRODUCTION-READINESS.md: `data/openclawn.db` tidak punya
mekanisme backup. Dipakai lewat `scripts/backup_db.py` (cron/systemd timer) atau
dipanggil langsung dari kode lain.

`sqlite3.Connection.backup()` (bukan `shutil.copy`/`cp` mentah) karena dijamin
aman dipanggil pada DB WAL yang sedang aktif dipakai proses server lain — API ini
mengambil snapshot konsisten via SQLite Online Backup API, bukan menyalin byte
file yang berpotensi setengah-tertulis.
"""

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

BACKUP_TIMESTAMP_FMT = "%Y%m%dT%H%M%S"


def backup_database(source_path: str, backup_dir: str) -> Path:
    """Salin `source_path` ke `backup_dir/openclawn_{timestamp}.db`, return path tujuan.

    Aman dipanggil selagi server (koneksi WAL lain) masih hidup. `FileNotFoundError`
    bila source tidak ada — kegagalan eksplisit lebih baik daripada backup kosong senyap.
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    dest_dir = Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime(BACKUP_TIMESTAMP_FMT)
    dest = dest_dir / f"openclawn_{ts}.db"

    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    return dest


def list_backups(backup_dir: str) -> list[Path]:
    """List file backup di `backup_dir`, terbaru dulu. Direktori tak ada → list kosong."""
    dest_dir = Path(backup_dir)
    if not dest_dir.exists():
        return []
    return sorted(dest_dir.glob("openclawn_*.db"), key=lambda p: p.name, reverse=True)


def prune_old_backups(backup_dir: str, keep: int) -> list[Path]:
    """Hapus backup selain `keep` yang terbaru. Return list path yang dihapus.

    Retensi sederhana berbasis JUMLAH file (bukan umur) — lebih mudah diprediksi
    untuk operator self-host dibanding "hapus yang lebih tua dari N hari" saat
    frekuensi backup bisa berubah-ubah.
    """
    backups = list_backups(backup_dir)
    to_remove = backups[keep:]
    for path in to_remove:
        path.unlink()
    return to_remove
