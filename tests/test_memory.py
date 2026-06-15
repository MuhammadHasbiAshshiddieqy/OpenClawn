"""Tests untuk MemoryManager — L1/L2/L4 layers, FTS5 search, CRUD."""

import pytest
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.layers import MemoryManager


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


@pytest.fixture
def memory(db):
    return MemoryManager(role="pm", session_id="test-session", db=db)


# ── L1: Key-value state ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l1_checkpoint_upsert(memory, db):
    """update_checkpoint harus insert pertama kali, lalu update di panggilan berikutnya."""
    await memory.update_checkpoint("ringkasan pertama")
    rows = await db.fetchall("SELECT * FROM memory_l1 WHERE role='pm'")
    assert len(rows) == 1
    assert rows[0]["value"] == "ringkasan pertama"

    await memory.update_checkpoint("ringkasan kedua")
    rows = await db.fetchall("SELECT * FROM memory_l1 WHERE role='pm'")
    assert len(rows) == 1  # tetap satu baris, ter-update
    assert rows[0]["value"] == "ringkasan kedua"


@pytest.mark.asyncio
async def test_l1_truncated_to_500(memory, db):
    """Value L1 harus dipotong ke 500 karakter."""
    long_text = "x" * 600
    await memory.update_checkpoint(long_text)
    rows = await db.fetchall("SELECT value FROM memory_l1 WHERE role='pm'")
    assert len(rows[0]["value"]) <= 500


@pytest.mark.asyncio
async def test_l1_load_context_returns_state(memory, db):
    """load_context harus mengembalikan L1 sebagai dict key-value."""
    await memory.update_checkpoint("state value")
    ctx = await memory.load_context(query="anything", skills=[])
    assert "last_summary" in ctx["l1"]
    assert ctx["l1"]["last_summary"] == "state value"


# ── L2: Facts ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l2_add_and_retrieve_fact(memory, db):
    """add_fact harus menyimpan dan load_context harus mengambilnya."""
    await memory.add_fact("user prefers dark mode", importance=3)
    await memory.add_fact("project uses SQLite", importance=5)

    ctx = await memory.load_context(query="anything", skills=[])
    assert len(ctx["l2"]) >= 2
    assert "user prefers dark mode" in ctx["l2"]
    assert "project uses SQLite" in ctx["l2"]


@pytest.mark.asyncio
async def test_l2_ordered_by_importance(memory, db):
    """L2 facts harus diurutkan berdasarkan importance DESC."""
    await memory.add_fact("low importance", importance=1)
    await memory.add_fact("high importance", importance=10)
    await memory.add_fact("medium importance", importance=5)

    ctx = await memory.load_context(query="anything", skills=[])
    # Fact dengan importance tertinggi harus muncul duluan
    assert ctx["l2"][0] == "high importance"


@pytest.mark.asyncio
async def test_l2_scoped_to_role(memory, db):
    """L2 facts hanya untuk role terkait, tidak bocor antar role."""
    await memory.add_fact("pm fact", importance=5)

    qa_memory = MemoryManager(role="qa", session_id="s2", db=db)
    ctx = await qa_memory.load_context(query="anything", skills=[])
    assert "pm fact" not in ctx["l2"]


# ── L3: Skills (passed in) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l3_skills_passed_through(memory):
    """L3 skills harus diteruskan apa adanya dari parameter."""
    skills = [
        {"skill_name": "auth-pattern", "skill_content": "..."},
        {"skill_name": "db-migration", "skill_content": "..."},
    ]
    ctx = await memory.load_context(query="auth", skills=skills)
    assert ctx["l3"] == skills
    assert len(ctx["l3"]) == 2


# ── L4: FTS5 cross-session search ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l4_archive_and_search(memory, db):
    """archive_session harus menyimpan dan FTS5 harus bisa mencari."""
    await memory.archive_session("fixed OAuth login bug", "full session content here")

    # FTS5 tidak melakukan stemming; gunakan kata yang sama persis ("fixed" bukan "fix")
    ctx = await memory.load_context(query="OAuth login bug fixed", skills=[])
    assert len(ctx["l4"]) >= 1
    assert "OAuth" in ctx["l4"][0]


@pytest.mark.asyncio
async def test_l4_search_triggered_by_specific_term(memory, db):
    """Query pendek dengan term spesifik (< 4 kata) tetap trigger FTS5 search."""
    await memory.archive_session("deploy pipeline bug", "details...")

    # "bug" adalah specific term, query 2 kata → tetap trigger FTS5
    ctx = await memory.load_context(query="bug deploy", skills=[])
    assert len(ctx["l4"]) >= 1


@pytest.mark.asyncio
async def test_l4_no_search_on_short_generic_query(memory, db):
    """Query pendek tanpa specific term tidak trigger FTS5, tidak crash."""
    await memory.archive_session("irrelevant session", "details...")

    # "halo apa" hanya 2 kata, tidak ada specific term → skip FTS5
    ctx = await memory.load_context(query="halo apa", skills=[])
    assert ctx["l4"] == []  # FTS5 tidak dipanggil


@pytest.mark.asyncio
async def test_l4_search_graceful_on_fts5_error(memory, db):
    """FTS5 syntax error tidak boleh crash — harus return []."""
    # FTS5 MATCH query dengan karakter khusus bisa error
    ctx = await memory.load_context(query='bug "login OR OAuth', skills=[])
    assert ctx["l4"] == []  # graceful skip


@pytest.mark.asyncio
async def test_l4_scoped_to_role(memory, db):
    """FTS5 search harus role-scoped, tidak bocor antar role."""
    await memory.archive_session("pm session about auth", "full content")

    qa_memory = MemoryManager(role="qa", session_id="s2", db=db)
    ctx = await qa_memory.load_context(query="auth session history", skills=[])
    # qa role tidak boleh melihat session pm
    assert len(ctx["l4"]) == 0


# ── _has_specific_term ──────────────────────────────────────────────────────


def test_has_specific_term_matches():
    """_has_specific_term harus mendeteksi kata kunci teknis."""
    mm = MemoryManager(role="pm", session_id="s", db=None)
    assert mm._has_specific_term("fix bug login") is True
    assert mm._has_specific_term("deploy api gateway") is True
    assert mm._has_specific_term("halo apa kabar") is False
    assert mm._has_specific_term("ERROR: crash detected") is True  # case insensitive


# ── load_context structure ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_context_returns_all_layers(memory):
    """load_context harus mengembalikan dict dengan keys l1, l2, l3, l4."""
    ctx = await memory.load_context(query="test", skills=[])
    assert set(ctx.keys()) == {"l1", "l2", "l3", "l4"}
    assert isinstance(ctx["l1"], dict)
    assert isinstance(ctx["l2"], list)
    assert isinstance(ctx["l3"], list)
    assert isinstance(ctx["l4"], list)
