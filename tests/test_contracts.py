"""Tests untuk Inovasi 4: Role Output Contracts + RoleNegotiator."""

import json
import pytest
from unittest.mock import AsyncMock
from infra.config import AppConfig
from infra.database import DatabaseManager
from roles.contracts import PMOutput, QAOutput, DevOutput, CONTRACT_REGISTRY
from roles.registry import RoleNegotiator


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


# ── Contract validation unit tests ──────────────────────────────────────────


def test_pm_output_valid():
    """PMOutput dengan data lengkap harus lolos validasi."""
    output = PMOutput(
        summary="Fitur login OAuth",
        user_stories=["Sebagai user, saya ingin login dengan Google"],
        acceptance_criteria=["Login berhasil dalam 3 detik"],
        priority="high",
    )
    assert output.summary == "Fitur login OAuth"
    assert output.priority == "high"


def test_pm_output_invalid_priority():
    """Priority di luar (low|medium|high) harus ditolak Pydantic."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PMOutput(summary="test", priority="critical")  # bukan nilai valid


def test_qa_output_valid():
    """QAOutput dengan data minimal harus lolos."""
    output = QAOutput(test_cases=["TC-001: login valid"])
    assert len(output.test_cases) == 1


def test_dev_output_requires_approach():
    """DevOutput tanpa field wajib 'approach' harus gagal."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DevOutput()  # approach wajib


def test_contract_registry_has_all_roles():
    """CONTRACT_REGISTRY harus punya semua role yang didefinisikan."""
    assert "pm" in CONTRACT_REGISTRY
    assert "qa" in CONTRACT_REGISTRY
    assert "dev" in CONTRACT_REGISTRY


# ── RoleNegotiator integration tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_output_passes_validation(db):
    """Output JSON valid sesuai contract → validation_ok=True, disimpan di DB."""
    negotiator = RoleNegotiator(db=db)

    valid_pm_json = json.dumps(
        {
            "summary": "Fitur baru",
            "user_stories": ["Story 1"],
            "acceptance_criteria": ["AC 1"],
            "priority": "medium",
            "open_questions": [],
        }
    )

    async def mock_agent_run(prompt: str):
        yield valid_pm_json

    mock_agent = AsyncMock()
    mock_agent.run = mock_agent_run

    result = await negotiator.handoff(
        session_id="s1",
        from_role="dev",
        to_role="pm",
        task_input="buat spec",
        agent_factory=lambda role: mock_agent,
    )

    assert result["valid"] is True
    assert result["output"]["summary"] == "Fitur baru"

    # Pastikan tersimpan di DB
    row = await db.fetchone("SELECT validation_ok FROM role_handoffs WHERE session_id='s1'")
    assert row["validation_ok"] == 1


@pytest.mark.asyncio
async def test_invalid_output_does_not_crash(db):
    """Output JSON tidak valid → validation_ok=False, tidak raise exception."""
    negotiator = RoleNegotiator(db=db)

    async def mock_agent_run(prompt: str):
        yield "ini bukan json!!!"

    mock_agent = AsyncMock()
    mock_agent.run = mock_agent_run

    result = await negotiator.handoff(
        session_id="s2",
        from_role="dev",
        to_role="qa",
        task_input="review kode",
        agent_factory=lambda role: mock_agent,
    )

    assert result["valid"] is False
    assert "error" in result["output"]

    row = await db.fetchone("SELECT validation_ok FROM role_handoffs WHERE session_id='s2'")
    assert row["validation_ok"] == 0


@pytest.mark.asyncio
async def test_unknown_role_returns_error(db):
    """Handoff ke role yang tidak ada di registry → error dict, tidak crash."""
    negotiator = RoleNegotiator(db=db)
    result = await negotiator.handoff(
        session_id="s3",
        from_role="pm",
        to_role="unknown_role",
        task_input="task",
        agent_factory=lambda role: None,
    )
    assert "error" in result
