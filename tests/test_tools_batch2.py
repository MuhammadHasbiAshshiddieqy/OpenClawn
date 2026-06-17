"""Test batch tool ke-2: file_append, apply_patch, pdf_read, db_query,
memory_search, json_query. DB :memory:, tanpa LLM/jaringan nyata."""

import dataclasses

import pytest

from infra.config import AppConfig, CONFIG
from infra.database import DatabaseManager
from tools.data import DbQueryTool, JsonQueryTool, MemorySearchTool
from tools.document import PdfReadTool
from tools.file_ops import ApplyPatchTool, FileAppendTool


@pytest.fixture
def ws(tmp_path, monkeypatch):
    patched = dataclasses.replace(CONFIG, workspace_root=str(tmp_path))
    for mod in ("tools.file_ops", "tools.document"):
        monkeypatch.setattr(f"{mod}.CONFIG", patched)
    return tmp_path


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


# ── file_append ─────────────────────────────────────────────────────────────


async def test_file_append_creates_and_appends(ws):
    t = FileAppendTool()
    await t.execute({"path": "log.txt", "content": "baris1\n"}, vault=None)
    await t.execute({"path": "log.txt", "content": "baris2\n"}, vault=None)
    assert (ws / "log.txt").read_text() == "baris1\nbaris2\n"


async def test_file_append_blocks_outside(ws):
    res = await FileAppendTool().execute({"path": "../x", "content": "y"}, vault=None)
    assert "error" in res


# ── apply_patch (atomik) ──────────────────────────────────────────────────────


async def test_apply_patch_multiple_edits(ws):
    (ws / "m.py").write_text("a = 1\nb = 2\nc = 3\n")
    res = await ApplyPatchTool().execute(
        {
            "path": "m.py",
            "edits": [
                {"old_string": "a = 1", "new_string": "a = 10"},
                {"old_string": "c = 3", "new_string": "c = 30"},
            ],
        },
        vault=None,
    )
    assert res["edits_applied"] == 2
    assert (ws / "m.py").read_text() == "a = 10\nb = 2\nc = 30\n"


async def test_apply_patch_atomic_rollback(ws):
    """Bila satu edit gagal, TIDAK ada perubahan ditulis."""
    original = "x = 1\ny = 2\n"
    (ws / "n.py").write_text(original)
    res = await ApplyPatchTool().execute(
        {
            "path": "n.py",
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 9"},
                {"old_string": "ABSENT", "new_string": "z"},  # gagal
            ],
        },
        vault=None,
    )
    assert "error" in res
    assert (ws / "n.py").read_text() == original  # tidak setengah ter-edit


# ── pdf_read ──────────────────────────────────────────────────────────────────


async def test_pdf_read_extracts_text(ws):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    pdf_path = ws / "doc.pdf"
    with open(pdf_path, "wb") as f:
        writer.write(f)

    res = await PdfReadTool().execute({"path": "doc.pdf"}, vault=None)
    assert "error" not in res
    assert res["pages"] == 1


async def test_pdf_read_missing_file(ws):
    res = await PdfReadTool().execute({"path": "tidak_ada.pdf"}, vault=None)
    assert "error" in res


# ── db_query (SELECT-only) ────────────────────────────────────────────────────


async def test_db_query_select_ok(db):
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content) VALUES (?,?,?)",
        ("dev", "s1", "isi skill"),
    )
    res = await DbQueryTool().execute({"sql": "SELECT skill_name FROM skills"}, vault=None, db=db)
    assert res["count"] == 1
    assert res["rows"][0]["skill_name"] == "s1"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM skills",
        "UPDATE skills SET role='x'",
        "DROP TABLE skills",
        "INSERT INTO skills (role) VALUES ('x')",
        "SELECT 1; DROP TABLE skills",
    ],
)
async def test_db_query_rejects_writes(db, sql):
    res = await DbQueryTool().execute({"sql": sql}, vault=None, db=db)
    assert "error" in res


async def test_db_query_no_db():
    res = await DbQueryTool().execute({"sql": "SELECT 1"}, vault=None, db=None)
    assert "error" in res


# ── memory_search ─────────────────────────────────────────────────────────────


async def test_memory_search_finds_skill(db):
    await db.execute(
        "INSERT INTO skills (role, skill_name, skill_content) VALUES (?,?,?)",
        ("dev", "deploy", "cara deploy ke produksi"),
    )
    res = await MemorySearchTool().execute(
        {"query": "deploy", "table": "skills"}, vault=None, db=db
    )
    assert res["count"] == 1


async def test_memory_search_rejects_bad_table(db):
    res = await MemorySearchTool().execute(
        {"query": "x", "table": "routing_events"}, vault=None, db=db
    )
    assert "error" in res


# ── json_query ────────────────────────────────────────────────────────────────


async def test_json_query_dot_path():
    data = '{"results": [{"title": "Halo"}]}'
    res = await JsonQueryTool().execute({"json": data, "path": "results.0.title"}, vault=None)
    assert res["value"] == "Halo"


async def test_json_query_object_input():
    res = await JsonQueryTool().execute({"json": {"a": {"b": 42}}, "path": "a.b"}, vault=None)
    assert res["value"] == 42


async def test_json_query_bad_path():
    res = await JsonQueryTool().execute({"json": '{"a": 1}', "path": "a.b.c"}, vault=None)
    assert "error" in res


async def test_json_query_invalid_json():
    res = await JsonQueryTool().execute({"json": "{not json", "path": "a"}, vault=None)
    assert "error" in res
