"""Test tool report_blocker (proactive blocker reporting) + tampil di linimasa."""

import pytest

from core.activity import ActivityTimeline
from infra.config import AppConfig
from infra.database import DatabaseManager
from tools.blocker import ReportBlockerTool


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


async def test_report_blocker_persists(db):
    tool = ReportBlockerTool()
    res = await tool.execute(
        {"_session_id": "s1", "_role": "dev", "summary": "kredensial hilang", "severity": "high"},
        vault=None,
        db=db,
    )
    assert res["ok"] is True
    row = await db.fetchone("SELECT * FROM agent_blockers WHERE session_id='s1'")
    assert row["summary"] == "kredensial hilang"
    assert row["severity"] == "high"
    assert row["status"] == "open"
    assert row["role"] == "dev"


async def test_report_blocker_no_approval():
    """report_blocker menulis tabel internal → tidak butuh approval."""
    assert ReportBlockerTool().requires_approval is False


async def test_report_blocker_requires_session(db):
    res = await ReportBlockerTool().execute({"summary": "x"}, vault=None, db=db)
    assert "error" in res


async def test_report_blocker_requires_summary(db):
    res = await ReportBlockerTool().execute(
        {"_session_id": "s1", "_role": "dev", "summary": "  "}, vault=None, db=db
    )
    assert "error" in res


async def test_report_blocker_rejects_bad_severity(db):
    res = await ReportBlockerTool().execute(
        {"_session_id": "s1", "_role": "dev", "summary": "x", "severity": "kritis"},
        vault=None,
        db=db,
    )
    assert "error" in res
    # Tidak menulis baris saat severity invalid.
    row = await db.fetchone("SELECT COUNT(*) AS n FROM agent_blockers")
    assert row["n"] == 0


async def test_report_blocker_default_severity_medium(db):
    await ReportBlockerTool().execute(
        {"_session_id": "s1", "_role": "qa", "summary": "ambigu"}, vault=None, db=db
    )
    row = await db.fetchone("SELECT severity FROM agent_blockers WHERE session_id='s1'")
    assert row["severity"] == "medium"


async def test_blocker_appears_in_timeline(db):
    await ReportBlockerTool().execute(
        {"_session_id": "s1", "_role": "dev", "summary": "dep mati", "severity": "high"},
        vault=None,
        db=db,
    )
    events = await ActivityTimeline(db).recent()
    blocker = next((e for e in events if e["kind"] == "blocker"), None)
    assert blocker is not None
    assert blocker["title"] == "dep mati"
    assert blocker["outcome"] == "high/open"
    assert blocker["role"] == "dev"
