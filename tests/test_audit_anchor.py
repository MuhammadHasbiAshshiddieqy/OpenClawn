"""Tests untuk core/audit_anchor.py — anchoring audit chain (TODO.md § Prioritas
9.1 follow-up).

Fokus test ini: membuktikan anchoring benar-benar menangkap DUA serangan yang
AuditChain.verify() SENDIRIAN tidak bisa tangkap (didokumentasikan eksplisit di
core/audit_chain.py dan dikunci di tests/test_audit_chain.py) — truncation
entry terakhir, dan penulisan-ulang seluruh rantai.
"""

from datetime import UTC, datetime, timedelta

import pytest

from core.audit_anchor import verify_against_anchors, write_anchor
from core.audit_chain import (
    ENTRY_ROUTING_DECISION,
    AuditChain,
    _canonical_body,
    canonical_payload,
    compute_hash,
)
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.retention import MIN_RETENTION_DAYS


async def _append_backdated(db, entry_type: str, payload: dict, days_old: int) -> str:
    """Sisipkan entry rantai dengan `created_at` TUA agar lolos trigger retensi
    (§ Prioritas 9.1 follow-up, infra/retention.py) saat test perlu men-DELETE-nya
    untuk mensimulasikan tampering. Pola sama `tests/test_audit_chain.py`."""
    created_at = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    payload_json = canonical_payload(payload)
    prev_row = await db.fetchone("SELECT record_hash FROM audit_chain ORDER BY id DESC LIMIT 1")
    prev_hash = prev_row["record_hash"] if prev_row else ""
    body = _canonical_body(entry_type, "", None, payload_json, created_at)
    record_hash = compute_hash(body, prev_hash)
    await db.execute(
        """INSERT INTO audit_chain
           (entry_type, ref_table, ref_id, payload_json, created_at, prev_hash, record_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (entry_type, "", None, payload_json, created_at, prev_hash, record_hash),
    )
    return record_hash


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


# ── write_anchor ──────────────────────────────────────────────────────────────


async def test_write_anchor_returns_none_for_empty_chain(db, tmp_path):
    anchor_path = tmp_path / "anchors.jsonl"
    assert await write_anchor(db, str(anchor_path)) is None
    assert not anchor_path.exists()


async def test_write_anchor_creates_file_and_entry(db, tmp_path):
    anchor_path = tmp_path / "anchors.jsonl"
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})

    entry = await write_anchor(db, str(anchor_path))
    assert entry is not None
    assert anchor_path.exists()
    assert entry["id"] == 1
    assert len(entry["record_hash"]) == 64


async def test_write_anchor_idempotent_without_new_activity(db, tmp_path):
    """Panggilan berulang (mis. cron tiap jam) tanpa aktivitas baru TIDAK
    menumpuk baris anchor duplikat."""
    anchor_path = tmp_path / "anchors.jsonl"
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})

    first = await write_anchor(db, str(anchor_path))
    second = await write_anchor(db, str(anchor_path))
    assert first is not None
    assert second is None
    assert anchor_path.read_text().strip().count("\n") == 0  # cuma 1 baris


async def test_write_anchor_writes_new_entry_after_new_activity(db, tmp_path):
    anchor_path = tmp_path / "anchors.jsonl"
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})
    await write_anchor(db, str(anchor_path))

    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 2})
    second = await write_anchor(db, str(anchor_path))
    assert second is not None
    assert len(anchor_path.read_text().strip().splitlines()) == 2


# ── verify_against_anchors ────────────────────────────────────────────────────


async def test_verify_against_anchors_no_file_is_ok(db, tmp_path):
    """File belum ada = belum pernah di-anchor, BUKAN indikasi rantai rusak."""
    result = await verify_against_anchors(db, str(tmp_path / "does_not_exist.jsonl"))
    assert result["ok"] is True
    assert result["anchors_checked"] == 0


async def test_verify_against_anchors_passes_when_chain_untouched(db, tmp_path):
    anchor_path = tmp_path / "anchors.jsonl"
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 2})
    await write_anchor(db, str(anchor_path))

    result = await verify_against_anchors(db, str(anchor_path))
    assert result["ok"] is True
    assert result["anchors_checked"] == 1
    assert result["failed"] == []


async def test_verify_against_anchors_catches_truncation(db, tmp_path):
    """Skenario yang TIDAK tertangkap AuditChain.verify() sendirian — lihat
    tests/test_audit_chain.py::test_detects_deleted_last_entry_only_via_anchor.
    Ini justru pembuktian bahwa anchoring menutup celah itu.

    Entry id=2 dibuat TUA (`_append_backdated`) agar lolos trigger retensi
    (§ Prioritas 9.1 follow-up) — untuk data < 180 hari, trigger itu SENDIRI
    sudah mencegah DELETE ini sama sekali; batas jaminan yang diuji di sini
    hanya relevan untuk data yang sudah di luar jendela retensi."""
    anchor_path = tmp_path / "anchors.jsonl"
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})
    await _append_backdated(db, ENTRY_ROUTING_DECISION, {"a": 2}, days_old=MIN_RETENTION_DAYS + 5)
    await write_anchor(db, str(anchor_path))

    await db.execute("DELETE FROM audit_chain WHERE id=2")
    assert (await AuditChain(db).verify())["ok"] is True  # verify() sendirian TAK menangkap ini

    result = await verify_against_anchors(db, str(anchor_path))
    assert result["ok"] is False
    assert result["anchors_checked"] == 1
    assert "hilang" in result["failed"][0]["problem"]


async def test_verify_against_anchors_catches_full_chain_rewrite(db, tmp_path):
    """Skenario kedua yang tak tertangkap verify() sendirian — lihat
    tests/test_audit_chain.py::test_rewriting_whole_chain_is_not_detected_by_verify_alone.

    Entry awal dibuat TUA supaya `DELETE FROM audit_chain` lolos trigger
    retensi — sama alasan test di atas."""
    anchor_path = tmp_path / "anchors.jsonl"
    await _append_backdated(db, ENTRY_ROUTING_DECISION, {"a": 1}, days_old=MIN_RETENTION_DAYS + 5)
    await write_anchor(db, str(anchor_path))

    await db.execute("DELETE FROM audit_chain")
    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 999})  # id=1 lagi, isi beda

    assert (await AuditChain(db).verify())["ok"] is True  # tetap lolos verify() sendirian

    result = await verify_against_anchors(db, str(anchor_path))
    assert result["ok"] is False
    assert "tak cocok" in result["failed"][0]["problem"]


async def test_multiple_anchors_all_checked(db, tmp_path):
    anchor_path = tmp_path / "anchors.jsonl"
    for i in range(3):
        await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": i})
        await write_anchor(db, str(anchor_path))

    result = await verify_against_anchors(db, str(anchor_path))
    assert result["ok"] is True
    assert result["anchors_checked"] == 3
