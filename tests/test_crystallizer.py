"""Tests untuk Inovasi 3: Confidence Crystallization + evaluator gating."""

import pytest
from unittest.mock import AsyncMock
from core.crystallizer import ConfidenceCrystallizer, EVALUATOR_FOR
from infra.config import AppConfig
from infra.database import DatabaseManager


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


def _mock_llm(confidence: int = 5, critical_gaps: bool = False):
    """LLM mock yang mengembalikan JSON evaluasi dengan confidence tertentu."""

    async def _stream(provider, model, messages, tools=None, max_tokens=4096):
        from core.llm_client import LLMChunk

        yield LLMChunk(
            type="text",
            text=f'{{"confidence": {confidence}, "critical_gaps": {str(critical_gaps).lower()}, "reasoning": "test"}}',
        )

    mock = AsyncMock()
    mock.stream_with_fallback = _stream
    return mock


def test_evaluator_at_least_as_strong_as_generator():
    """Audit #4: evaluator tidak boleh lebih lemah dari generator."""
    # Sonnet generator → evaluator harus Sonnet juga
    assert EVALUATOR_FOR["claude-sonnet-4-6"] == ("anthropic", "claude-sonnet-4-6")
    # Haiku generator → evaluator minimal Haiku
    assert EVALUATOR_FOR["claude-haiku-4-5-20251001"] == ("anthropic", "claude-haiku-4-5-20251001")
    # e4b generator → evaluator naik ke 12b
    assert EVALUATOR_FOR["gemma4:e4b"][1] == "gemma4:12b"
    # e2b generator → evaluator naik ke e4b
    assert EVALUATOR_FOR["gemma4:e2b"][1] == "gemma4:e4b"
    # 12b generator → evaluator naik ke Haiku (cloud)
    assert EVALUATOR_FOR["gemma4:12b"][0] == "anthropic"


@pytest.mark.asyncio
async def test_high_confidence_crystallizes_as_active(db):
    """Confidence >= 4 dan tidak ada critical gaps → status active."""
    llm = _mock_llm(confidence=5, critical_gaps=False)
    c = ConfidenceCrystallizer(role="pm", llm=llm, db=db)

    result = await c.crystallize(
        task="buat fitur login",
        solution="implementasi JWT auth",
        history=[],
        generator_model="claude-sonnet-4-6",
    )
    assert result["status"] == "active"


@pytest.mark.asyncio
async def test_low_confidence_crystallizes_as_draft(db):
    """Confidence < 4 → status draft, bukan active."""
    llm = _mock_llm(confidence=2, critical_gaps=False)
    c = ConfidenceCrystallizer(role="pm", llm=llm, db=db)

    result = await c.crystallize(
        task="analisis kebutuhan sistem",
        solution="solusi belum lengkap",
        history=[],
        generator_model="gemma4:e4b",
    )
    assert result["status"] == "draft"


@pytest.mark.asyncio
async def test_critical_gaps_forces_draft(db):
    """Adanya critical_gaps → status draft meskipun confidence tinggi."""
    llm = _mock_llm(confidence=5, critical_gaps=True)
    c = ConfidenceCrystallizer(role="pm", llm=llm, db=db)

    result = await c.crystallize(
        task="deploy ke production",
        solution="solusi dengan gap kritis",
        history=[],
        generator_model="claude-haiku-4-5-20251001",
    )
    assert result["status"] == "draft"


@pytest.mark.asyncio
async def test_parse_failure_defaults_to_draft(db):
    """Jika LLM mengembalikan JSON tidak valid → fail-safe ke confidence rendah (draft)."""

    async def _bad_stream(provider, model, messages, tools=None, max_tokens=4096):
        from core.llm_client import LLMChunk

        yield LLMChunk(type="text", text="bukan json sama sekali!!!")

    mock = AsyncMock()
    mock.stream_with_fallback = _bad_stream
    c = ConfidenceCrystallizer(role="pm", llm=mock, db=db)

    result = await c.crystallize(
        task="tugas apapun",
        solution="solusi",
        history=[],
        generator_model="gemma4:e2b",
    )
    assert result["status"] == "draft"


def test_slug_generates_valid_name():
    """_slug menghasilkan nama yang konsisten dari task string."""
    from core.crystallizer import ConfidenceCrystallizer

    c = ConfidenceCrystallizer(role="pm", llm=None, db=None)
    assert c._slug("buat fitur login user") == "buat-fitur-login-user"
    assert c._slug("") == "unnamed-skill"


# ── observability: crystallization_log (Inovasi 3 kasat mata) ─────────────────


@pytest.mark.asyncio
async def test_crystallization_logged_with_decision(db):
    """Setiap percobaan dicatat ke crystallization_log: status, confidence, model."""
    llm = _mock_llm(confidence=5, critical_gaps=False)
    c = ConfidenceCrystallizer(role="dev", llm=llm, db=db)
    await c.crystallize(
        task="buat parser csv",
        solution="pakai modul csv",
        history=[],
        generator_model="claude-sonnet-4-6",
    )
    row = await db.fetchone("SELECT * FROM crystallization_log WHERE role='dev'")
    assert row is not None
    assert row["status"] == "active"
    assert row["confidence"] == 5
    assert row["critical_gaps"] == 0
    assert row["generator_model"] == "claude-sonnet-4-6"
    # evaluator minimal setara generator (sonnet → sonnet)
    assert row["evaluator_model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_crystallization_log_records_draft(db):
    """Draft (confidence rendah) juga tercatat — itu justru yang menarik untuk ditinjau."""
    llm = _mock_llm(confidence=2, critical_gaps=False)
    c = ConfidenceCrystallizer(role="qa", llm=llm, db=db)
    await c.crystallize(
        task="evaluasi tes", solution="belum lengkap", history=[], generator_model="gemma4:e4b"
    )
    row = await db.fetchone("SELECT status, confidence FROM crystallization_log WHERE role='qa'")
    assert row["status"] == "draft"
    assert row["confidence"] == 2
