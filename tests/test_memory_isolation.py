"""Audit 2026-09-25 (#2, kritis): memori L1/L4 tak boleh bocor antar sesi/user.

Temuan asli: checkpoint L1 `last_summary` satu baris per ROLE — jawaban terakhir
user A (500 char) disuntik ke prompt user B yang memakai role sama, tiap turn.
Arsip L4 (FTS) juga dicari lintas semua sesi satu role.
"""

import pytest

from infra.chat_sessions import ChatSessionStore
from infra.config import AppConfig
from infra.database import DatabaseManager
from memory.layers import MemoryManager
from tools.data import MemorySearchTool


@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_l1_checkpoint_not_visible_to_other_session(db):
    await MemoryManager("pm", "sess-A", db, user_id="1").update_checkpoint("rahasia A")
    ctx_b = await MemoryManager("pm", "sess-B", db, user_id="2").load_context("halo", [])
    assert "rahasia A" not in str(ctx_b["l1"])
    ctx_a = await MemoryManager("pm", "sess-A", db, user_id="1").load_context("halo", [])
    assert ctx_a["l1"].get("last_summary") == "rahasia A"


@pytest.mark.asyncio
async def test_legacy_global_checkpoint_ignored(db):
    """Baris `last_summary` global dari sebelum perbaikan tak lagi disuntik."""
    await db.execute(
        "INSERT INTO memory_l1 (role, key, value) VALUES ('pm','last_summary','lama bocor')"
    )
    ctx = await MemoryManager("pm", "sess-new", db).load_context("halo", [])
    assert "lama bocor" not in str(ctx["l1"])


@pytest.mark.asyncio
async def test_non_checkpoint_l1_keys_still_shared_per_role(db):
    """Fakta role non-checkpoint tetap berlaku (perilaku lama dipertahankan)."""
    await db.execute("INSERT INTO memory_l1 (role, key, value) VALUES ('pm','mode','review')")
    ctx = await MemoryManager("pm", "any", db).load_context("halo", [])
    assert ctx["l1"].get("mode") == "review"


async def _archive(db, session_id: str, owner: str | None, text: str) -> None:
    await ChatSessionStore(db).ensure_created(session_id, "pm", owner_user_id=owner)
    await MemoryManager("pm", session_id, db).archive_session(summary=text, full_content=text)


@pytest.mark.asyncio
async def test_l4_archive_scoped_to_owner(db):
    await _archive(db, "sess-A", "1", "deploy bug database rahasia milik A")
    await _archive(db, "sess-B", "2", "deploy bug database milik B")

    ctx_b = await MemoryManager("pm", "sess-B2", db, user_id="2").load_context(
        "deploy bug database", []
    )
    assert ctx_b["l4"] and all("milik A" not in s for s in ctx_b["l4"])
    assert any("milik B" in s for s in ctx_b["l4"])


@pytest.mark.asyncio
async def test_l4_default_user_excludes_owned_sessions(db):
    """Auth nonaktif ('default'): sesi tanpa owner terlihat, sesi milik user login tidak."""
    await _archive(db, "sess-owned", "7", "deploy bug database milik user 7")
    await _archive(db, "sess-anon", None, "deploy bug database anonim")
    ctx = await MemoryManager("pm", "x", db).load_context("deploy bug database", [])
    assert any("anonim" in s for s in ctx["l4"])
    assert all("user 7" not in s for s in ctx["l4"])


@pytest.mark.asyncio
async def test_memory_search_l1_hides_other_sessions(db):
    await MemoryManager("pm", "sess-A", db).update_checkpoint("rahasia sesi A")
    result = await MemorySearchTool().execute(
        {"query": "rahasia", "table": "memory_l1", "_role": "pm", "_session_id": "sess-B"},
        vault=None,
        db=db,
    )
    assert result["results"] == []


@pytest.mark.asyncio
async def test_deleting_chat_removes_l4_archive_and_l1_checkpoint(db):
    """Audit 2026-09-25: "hapus chat" SEBELUMNYA meninggalkan transkrip penuh di
    memory_l4 (tetap dicari & disuntik ke prompt) dan checkpoint L1 sesi."""
    await _archive(db, "sess-del", None, "deploy bug database rahasia")
    await MemoryManager("pm", "sess-del", db).update_checkpoint("ringkasan rahasia")
    await ChatSessionStore(db).soft_delete("sess-del")
    assert await db.fetchall("SELECT 1 FROM memory_l4 WHERE session_id='sess-del'") == []
    assert await db.fetchall("SELECT 1 FROM memory_l1 WHERE key='last_summary:sess-del'") == []
