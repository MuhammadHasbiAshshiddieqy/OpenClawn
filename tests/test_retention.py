"""Tests untuk trigger retensi minimum 180 hari (TODO.md § Prioritas 9.1 follow-up,
EU AI Act Article 12) di `routing_events`, `approval_log`, `audit_chain`.

Penegakan SUNGGUHAN ada di trigger SQLite (`migrations/001_initial.sql`), bukan
kode Python — jadi test ini fokus membuktikan perilaku trigger langsung lewat
DB, bukan lewat pemanggilan fungsi Python.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.retention import MIN_RETENTION_DAYS


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


def _old_timestamp(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# ── routing_events ────────────────────────────────────────────────────────────


async def test_routing_events_young_row_cannot_be_deleted(db):
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text) VALUES (?,?,?)",
        ("s1", "dev", "q"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        await db.execute("DELETE FROM routing_events WHERE session_id='s1'")

    row = await db.fetchone("SELECT session_id FROM routing_events WHERE session_id='s1'")
    assert row is not None, "baris HARUS tetap ada setelah DELETE ditolak trigger"


async def test_routing_events_old_row_can_be_deleted(db):
    old_ts = _old_timestamp(MIN_RETENTION_DAYS + 5)
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text, created_at) VALUES (?,?,?,?)",
        ("s2", "dev", "q", old_ts),
    )
    await db.execute("DELETE FROM routing_events WHERE session_id='s2'")

    row = await db.fetchone("SELECT session_id FROM routing_events WHERE session_id='s2'")
    assert row is None


async def test_routing_events_row_exactly_at_boundary_is_still_protected(db):
    """179 hari (di bawah ambang) HARUS masih diblokir — batas eksklusif di
    sisi yang aman (fail-closed), bukan off-by-one yang longgar."""
    boundary_ts = _old_timestamp(MIN_RETENTION_DAYS - 1)
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text, created_at) VALUES (?,?,?,?)",
        ("s3", "dev", "q", boundary_ts),
    )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        await db.execute("DELETE FROM routing_events WHERE session_id='s3'")


# ── approval_log ──────────────────────────────────────────────────────────────


async def test_approval_log_young_row_cannot_be_deleted(db):
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, decision) VALUES (?,?,?)",
        ("s1", "code_run", "approved"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        await db.execute("DELETE FROM approval_log WHERE session_id='s1'")


async def test_approval_log_old_row_can_be_deleted(db):
    old_ts = _old_timestamp(MIN_RETENTION_DAYS + 5)
    await db.execute(
        "INSERT INTO approval_log (session_id, tool_name, decision, created_at) VALUES (?,?,?,?)",
        ("s2", "code_run", "approved", old_ts),
    )
    await db.execute("DELETE FROM approval_log WHERE session_id='s2'")

    row = await db.fetchone("SELECT session_id FROM approval_log WHERE session_id='s2'")
    assert row is None


# ── audit_chain ───────────────────────────────────────────────────────────────


async def test_audit_chain_young_row_cannot_be_deleted(db):
    from core.audit_chain import ENTRY_ROUTING_DECISION, AuditChain

    await AuditChain(db).append(ENTRY_ROUTING_DECISION, {"a": 1})
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        await db.execute("DELETE FROM audit_chain WHERE id=1")


async def test_audit_chain_old_row_can_be_deleted(db):
    old_ts = (datetime.now(UTC) - timedelta(days=MIN_RETENTION_DAYS + 5)).isoformat()
    await db.execute(
        """INSERT INTO audit_chain
           (entry_type, ref_table, ref_id, payload_json, created_at, prev_hash, record_hash)
           VALUES (?,?,?,?,?,?,?)""",
        ("routing.decision", "", None, "{}", old_ts, "", "deadbeef"),
    )
    await db.execute("DELETE FROM audit_chain WHERE id=1")

    row = await db.fetchone("SELECT id FROM audit_chain WHERE id=1")
    assert row is None


# ── perilaku fail-closed batch DELETE ─────────────────────────────────────────


async def test_batch_delete_mixing_old_and_young_rolls_back_entirely(db):
    """Satu statement DELETE yang mengenai baris tua DAN muda sekaligus harus
    di-ROLLBACK SELURUHNYA — termasuk baris tua yang sebenarnya boleh dihapus.
    Fail-closed (CLAUDE.md §1): lebih aman menolak semuanya daripada menghapus
    sebagian dan meninggalkan status ambigu."""
    old_ts = _old_timestamp(MIN_RETENTION_DAYS + 5)
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text, created_at) VALUES (?,?,?,?)",
        ("old", "dev", "q", old_ts),
    )
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text) VALUES (?,?,?)",
        ("young", "dev", "q"),
    )

    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        await db.execute("DELETE FROM routing_events WHERE session_id IN ('old', 'young')")

    old_row = await db.fetchone("SELECT session_id FROM routing_events WHERE session_id='old'")
    young_row = await db.fetchone("SELECT session_id FROM routing_events WHERE session_id='young'")
    assert old_row is not None, "baris tua ikut bertahan — statement di-rollback penuh"
    assert young_row is not None


# ── UPDATE tetap tidak terpengaruh (trigger hanya BEFORE DELETE) ─────────────


async def test_update_is_not_blocked_by_retention_trigger(db):
    """Trigger hanya menjaga DELETE — alur normal (finalize/resolve/dst yang
    memakai UPDATE) tidak boleh ikut terblokir."""
    await db.execute(
        "INSERT INTO routing_events (session_id, role, query_text) VALUES (?,?,?)",
        ("s1", "dev", "q"),
    )
    await db.execute(
        "UPDATE routing_events SET tokens_in=100 WHERE session_id='s1'"
    )  # tidak boleh raise

    row = await db.fetchone("SELECT tokens_in FROM routing_events WHERE session_id='s1'")
    assert row["tokens_in"] == 100
