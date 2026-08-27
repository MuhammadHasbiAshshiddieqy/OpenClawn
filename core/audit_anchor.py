"""Anchoring untuk audit chain (TODO.md § Prioritas 9.1 follow-up).

MENGAPA ADA: `AuditChain.verify()` (`core/audit_chain.py`) mendeteksi isi yang
diubah dan entry yang dihapus/disisipkan/ditukar urutan — TAPI, seperti
didokumentasikan eksplisit di modul itu, TIDAK mendeteksi dua hal: (1)
penghapusan entry TERAKHIR (truncation), dan (2) penulisan-ulang SELURUH
rantai dengan hash yang konsisten. Keduanya menghasilkan rantai yang secara
internal tetap valid — `verify()` tak punya cara membedakannya dari rantai
yang memang belum pernah lebih panjang dari itu.

Anchoring menutup celah itu dengan cara paling sederhana: simpan snapshot
`(id, record_hash)` rantai ke FILE TERPISAH dari database (JSON Lines,
append-only) secara berkala. Verifikasi lintas-anchor lalu mengecek: apakah
titik yang PERNAH di-anchor MASIH ada di tabel dengan hash yang SAMA PERSIS?
Truncation membuat baris itu hilang; rewrite penuh membuatnya hilang ATAU
berubah isi (SQLite menggunakan kembali id terkecil yang kosong untuk
`INTEGER PRIMARY KEY` biasa setelah tabel dikosongkan, jadi id yang sama bisa
muncul lagi dengan hash yang berbeda) — kedua kasus tertangkap perbandingan
hash sederhana, tanpa perlu tahu SERANGAN mana yang terjadi.

BATAS JAMINAN — jujur, sama semangat `core/audit_chain.py`: file anchor ini
by default berada di disk YANG SAMA dengan database. Penyerang dengan akses
tulis penuh ke filesystem bisa mengubah KEDUANYA secara konsisten. Nilai
mekanisme ini SEBELUM disalin off-host: (1) menangkap korupsi/bug, bukan cuma
serangan sengaja — jauh lebih mungkin terjadi dalam praktik; (2) format
berbeda (JSONL append-only vs tabel SQL) berarti penyerang harus tahu dan
mengubah DUA representasi berbeda secara konsisten, bukan satu. Nilai
SUNGGUHAN (anchoring independen dari mesin yang dijaga) baru didapat kalau
operator menyalin file ini off-host secara berkala — lewat cron yang sama
dengan `scripts/backup_db.py`, git, rsync, apa pun. Kebijakan "disalin ke
mana" tetap di luar scope modul ini, sama seperti `backup_db.py` tidak
mengatur ke mana hasil backup disalin.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.audit_chain import AuditChain
from infra.database import DatabaseManager


def _read_all_anchors(path: Path) -> list[dict]:
    """Baca semua baris anchor tersimpan. File belum ada → list kosong (bukan
    error — artinya belum pernah di-anchor, bukan rantai bermasalah)."""
    if not path.exists():
        return []
    anchors = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                anchors.append(json.loads(line))
    return anchors


async def write_anchor(db: DatabaseManager, anchor_path: str) -> dict | None:
    """Tulis satu baris anchor BARU ke `anchor_path` bila head rantai berbeda
    dari anchor terakhir tersimpan.

    Idempoten dengan sengaja: dipanggil berulang (mis. cron tiap jam) tanpa
    aktivitas baru di rantai TIDAK menumpuk baris anchor identik — hanya
    aktivitas baru yang menghasilkan entry anchor baru. Return dict anchor
    yang ditulis, atau `None` bila tak ada perubahan ATAU rantai masih kosong.
    """
    head = await AuditChain(db).head()
    if head is None:
        return None

    path = Path(anchor_path)
    existing = _read_all_anchors(path)
    if existing and existing[-1]["record_hash"] == head["record_hash"]:
        return None  # tak ada aktivitas baru sejak anchor terakhir

    entry = {
        "id": head["id"],
        "record_hash": head["record_hash"],
        "chain_created_at": head["created_at"],
        "anchored_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


async def verify_against_anchors(db: DatabaseManager, anchor_path: str) -> dict:
    """Cek tiap anchor tersimpan terhadap tabel `audit_chain` SEKARANG.

    Untuk tiap anchor `(id, record_hash)`: baris dengan `id` itu HARUS masih
    ada di tabel DENGAN `record_hash` yang SAMA PERSIS. Baris hilang →
    truncation atau rantai ditulis ulang dari titik sebelum `id` ini. Hash
    beda → isi baris itu berubah sejak di-anchor (termasuk kasus rewrite
    penuh yang kebetulan memakai `id` yang sama).

    Return `{"ok", "anchors_checked", "failed", "reason"}`. `anchors_checked=0`
    (file belum ada/kosong) BUKAN kegagalan — berarti belum pernah di-anchor,
    bukan rantai dirusak.
    """
    anchors = _read_all_anchors(Path(anchor_path))
    if not anchors:
        return {
            "ok": True,
            "anchors_checked": 0,
            "failed": [],
            "reason": "belum ada anchor tersimpan",
        }

    failed = []
    for anchor in anchors:
        row = await db.fetchone("SELECT record_hash FROM audit_chain WHERE id=?", (anchor["id"],))
        if row is None:
            failed.append(
                {
                    **anchor,
                    "problem": "entry hilang dari tabel (truncation atau rantai ditulis ulang)",
                }
            )
        elif row["record_hash"] != anchor["record_hash"]:
            failed.append({**anchor, "problem": "hash tak cocok (isi berubah sejak di-anchor)"})

    return {
        "ok": len(failed) == 0,
        "anchors_checked": len(anchors),
        "failed": failed,
        "reason": ""
        if not failed
        else f"{len(failed)} dari {len(anchors)} anchor tak cocok dengan tabel saat ini",
    }
