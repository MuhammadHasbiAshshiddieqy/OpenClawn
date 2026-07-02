"""Test untuk infra/backup.py — backup/restore SQLite (§ production-readiness).

Gap dicatat di PRODUCTION-READINESS.md §0 & TODO.md § Prioritas 1.5: tidak ada
mekanisme backup untuk data/openclawn.db. `sqlite3.Connection.backup()` dipakai
(bukan `cp` mentah) karena aman dipanggil pada DB WAL yang sedang dipakai proses
lain — snapshot konsisten tanpa perlu menghentikan server.
"""

import sqlite3
import time

import pytest

from infra.backup import backup_database, list_backups, prune_old_backups


def _make_db(path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO t (val) VALUES (?)", (f"row-{i}",))
    conn.commit()
    conn.close()


def test_backup_creates_file_with_same_data(tmp_path):
    src = tmp_path / "openclawn.db"
    _make_db(src, rows=5)
    backup_dir = tmp_path / "backups"

    dest = backup_database(str(src), str(backup_dir))

    assert dest.exists()
    conn = sqlite3.connect(dest)
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert count == 5


def test_backup_works_while_source_open_with_wal(tmp_path):
    """Backup harus konsisten walau koneksi WAL lain masih terbuka (server hidup)."""
    src = tmp_path / "openclawn.db"
    _make_db(src, rows=2)
    live_conn = sqlite3.connect(src)
    live_conn.execute("PRAGMA journal_mode=WAL")
    live_conn.execute("INSERT INTO t (val) VALUES ('live')")
    live_conn.commit()

    dest = backup_database(str(src), str(tmp_path / "backups"))

    conn = sqlite3.connect(dest)
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    live_conn.close()
    assert count == 3


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(str(tmp_path / "does_not_exist.db"), str(tmp_path / "backups"))


def test_backup_filename_is_timestamped_and_sortable(tmp_path):
    src = tmp_path / "openclawn.db"
    _make_db(src)

    dest1 = backup_database(str(src), str(tmp_path / "backups"))
    time.sleep(1.1)  # timestamp granularity: detik
    dest2 = backup_database(str(src), str(tmp_path / "backups"))

    assert dest1 != dest2
    assert sorted([dest1.name, dest2.name]) == [dest1.name, dest2.name]


def test_list_backups_returns_newest_first(tmp_path):
    src = tmp_path / "openclawn.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"

    d1 = backup_database(str(src), str(backup_dir))
    time.sleep(1.1)
    d2 = backup_database(str(src), str(backup_dir))

    found = list_backups(str(backup_dir))
    assert found[0] == d2
    assert found[1] == d1


def test_list_backups_empty_dir_returns_empty_list(tmp_path):
    assert list_backups(str(tmp_path / "nonexistent")) == []


def test_prune_old_backups_keeps_only_n_newest(tmp_path):
    src = tmp_path / "openclawn.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"

    made = []
    for _ in range(5):
        made.append(backup_database(str(src), str(backup_dir)))
        time.sleep(1.1)

    removed = prune_old_backups(str(backup_dir), keep=2)

    remaining = list_backups(str(backup_dir))
    assert len(remaining) == 2
    assert remaining == made[-1:-3:-1]  # dua terbaru, urutan newest-first
    assert len(removed) == 3


def test_prune_old_backups_noop_when_under_limit(tmp_path):
    src = tmp_path / "openclawn.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"
    backup_database(str(src), str(backup_dir))

    removed = prune_old_backups(str(backup_dir), keep=10)

    assert removed == []
    assert len(list_backups(str(backup_dir))) == 1
