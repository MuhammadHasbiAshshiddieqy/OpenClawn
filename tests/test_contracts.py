"""Tests untuk Inovasi 4: Role Output Contracts + RoleNegotiator."""

import json
import tomllib
from pathlib import Path
import pytest
from unittest.mock import AsyncMock
from pydantic import ValidationError
from infra.config import AppConfig
from infra.database import DatabaseManager
from core.agent_loop import AgentEvent
from roles.contracts import (
    PMOutput,
    QAOutput,
    DevOutput,
    DataOutput,
    SecurityOutput,
    CONTRACT_REGISTRY,
)
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


def test_data_output_valid():
    """DataOutput minimal lolos; confidence default 'medium'."""
    out = DataOutput(summary="Penjualan naik 12% QoQ")
    assert out.confidence == "medium"
    assert out.findings == []


def test_data_invalid_confidence():
    """confidence di luar (low|medium|high) ditolak."""
    with pytest.raises(ValidationError):
        DataOutput(summary="x", confidence="very-high")


def test_security_output_valid_and_risk_levels():
    """SecurityOutput lolos; risk_level menerima 'critical' (beda dari PM 'high')."""
    out = SecurityOutput(summary="2 secret ter-hardcode", pii_detected=True, risk_level="critical")
    assert out.pii_detected is True
    assert out.risk_level == "critical"


def test_security_invalid_risk_level():
    """risk_level di luar set yang diizinkan ditolak."""
    with pytest.raises(ValidationError):
        SecurityOutput(summary="x", risk_level="catastrophic")


def test_contract_registry_has_all_roles():
    """CONTRACT_REGISTRY harus punya semua role yang didefinisikan."""
    for role in ("pm", "qa", "dev", "data", "security"):
        assert role in CONTRACT_REGISTRY


# ── Soul loadability + konsistensi soul ↔ contract ──────────────────────────


def _role_dirs() -> list[Path]:
    return sorted(p.parent for p in Path("roles").glob("*/soul.toml"))


def test_all_souls_loadable_and_well_formed():
    """Setiap soul.toml bisa di-parse & punya field wajib (meta, system_prompt, tools)."""
    dirs = _role_dirs()
    assert dirs, "tidak ada soul.toml ditemukan"
    for d in dirs:
        with open(d / "soul.toml", "rb") as f:
            soul = tomllib.load(f)
        assert soul["meta"]["role"] == d.name, f"meta.role tidak cocok folder: {d.name}"
        assert soul["system_prompt"]["content"].strip(), f"system_prompt kosong: {d.name}"
        assert isinstance(soul["tools"]["allowed"], list) and soul["tools"]["allowed"]


def test_soul_output_type_matches_registry():
    """output_type di soul harus menunjuk contract yang ada untuk role itu."""
    for d in _role_dirs():
        with open(d / "soul.toml", "rb") as f:
            soul = tomllib.load(f)
        declared = soul.get("contract", {}).get("output_type")
        if not declared:
            continue
        contract = CONTRACT_REGISTRY.get(d.name)
        assert contract is not None, f"role {d.name} punya output_type tapi tak ada di registry"
        assert contract.__name__ == declared, (
            f"soul {d.name} output_type={declared} != {contract.__name__}"
        )


def test_security_role_is_read_only():
    """Role security tidak boleh punya tool yang menulis/eksekusi/network (advisory only)."""
    with open("roles/security/soul.toml", "rb") as f:
        soul = tomllib.load(f)
    allowed = set(soul["tools"]["allowed"])
    forbidden = {
        "file_write",
        "file_edit",
        "file_append",
        "apply_patch",
        "code_run",
        "shell_run",
        "http_request",
        "web_fetch",
        "web_search",
    }
    leaked = allowed & forbidden
    assert not leaked, f"role security seharusnya read-only, tapi punya: {leaked}"


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
        # run() yield AgentEvent (bukan str) — sesuai loop nyata.
        yield AgentEvent(type="token", text=valid_pm_json)

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
        yield AgentEvent(type="token", text="ini bukan json!!!")

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
