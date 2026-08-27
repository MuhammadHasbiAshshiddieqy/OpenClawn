"""Sandbox proyek besar/kompleks (§ Prioritas 8.3) — image kustom per-proyek dengan
dependency Python di-*bake* saat `docker build`, network HANYA terbuka di situ, TIDAK
PERNAH saat `docker run` eksekusi kode sungguhan (keputusan owner: opsi (a) dari 3
kandidat TODO.md). Docker di-mock di seluruh file ini (pola sama `test_tools.py`) —
tidak ada network/build sungguhan di suite pytest.

Empat bagian:
1. `SessionSandboxImageStore` (infra/sandbox_image.py): baca/tulis image aktif per-sesi.
2. `_validate_requirements` (tools/sandbox_image.py): validasi keamanan ringan.
3. `DockerSandbox.build_project_image` (tools/sandbox.py): cache, build, timeout, gagal.
4. `BuildSandboxImageTool` + integrasi `AgentLoop`: image terpakai otomatis lintas turn.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent_loop import AgentConfig, AgentLoop, _TRUST_MODE_EXEMPT
from core.llm_client import LLMChunk
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.sandbox_image import CURRENT_SANDBOX_IMAGE, SessionSandboxImageStore
from tools.sandbox import DockerSandbox, SandboxUnavailable
from tools.sandbox_image import BuildSandboxImageTool, _validate_requirements


def _set_workspace(monkeypatch, path):
    """Arahkan workspace_root `tools.sandbox_image` ke `path` (CONFIG frozen →
    ganti referensi) — pola sama `tests/test_tools.py::_set_workspace`."""
    import dataclasses

    from infra.config import CONFIG

    patched = dataclasses.replace(CONFIG, workspace_root=str(path))
    monkeypatch.setattr("tools.sandbox_image.CONFIG", patched)


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


# ── SessionSandboxImageStore ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_sandbox_image_get_set_roundtrip(db):
    store = SessionSandboxImageStore(db)
    assert await store.get("s1") is None
    await store.set("s1", "openclawn-sandbox-proj:abc123")
    assert await store.get("s1") == "openclawn-sandbox-proj:abc123"


@pytest.mark.asyncio
async def test_session_sandbox_image_upsert_overwrites(db):
    store = SessionSandboxImageStore(db)
    await store.set("s1", "openclawn-sandbox-proj:aaa")
    await store.set("s1", "openclawn-sandbox-proj:bbb")
    assert await store.get("s1") == "openclawn-sandbox-proj:bbb"


@pytest.mark.asyncio
async def test_session_sandbox_image_isolated_per_session(db):
    store = SessionSandboxImageStore(db)
    await store.set("s1", "openclawn-sandbox-proj:aaa")
    assert await store.get("s2") is None


def test_effective_sandbox_image_falls_back_to_default():
    token = CURRENT_SANDBOX_IMAGE.set(None)
    try:
        from infra.sandbox_image import effective_sandbox_image

        assert effective_sandbox_image("openclawn-sandbox:latest") == "openclawn-sandbox:latest"
    finally:
        CURRENT_SANDBOX_IMAGE.reset(token)


def test_effective_sandbox_image_uses_override_when_set():
    token = CURRENT_SANDBOX_IMAGE.set("openclawn-sandbox-proj:abc")
    try:
        from infra.sandbox_image import effective_sandbox_image

        assert effective_sandbox_image("openclawn-sandbox:latest") == "openclawn-sandbox-proj:abc"
    finally:
        CURRENT_SANDBOX_IMAGE.reset(token)


# ── _validate_requirements ────────────────────────────────────────────────────


def test_validate_requirements_rejects_empty():
    assert _validate_requirements("") is not None
    assert _validate_requirements("   \n\n  ") is not None


def test_validate_requirements_accepts_plain_packages():
    assert _validate_requirements("requests==2.31.0\nnumpy>=1.26\n# comment\n\n") is None


def test_validate_requirements_rejects_pip_option_lines():
    for bad in (
        "-e git+https://example.com/evil.git",
        "--index-url https://evil.example.com/simple",
        "-i https://evil.example.com/simple",
        "-r other-requirements.txt",
        "--extra-index-url https://evil.example.com",
    ):
        content = f"requests==2.31.0\n{bad}\n"
        err = _validate_requirements(content)
        assert err is not None, f"harus ditolak: {bad!r}"


def test_validate_requirements_rejects_too_many_lines():
    content = "\n".join(f"pkg{i}==1.0" for i in range(300))
    assert _validate_requirements(content) is not None


def test_validate_requirements_rejects_too_large():
    content = "requests==2.31.0\n" * 3000  # jauh melebihi 20_000 byte
    assert _validate_requirements(content) is not None


# ── DockerSandbox.build_project_image ─────────────────────────────────────────


def _fake_proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=None)
    proc.kill = MagicMock()
    return proc


@pytest.mark.asyncio
async def test_build_project_image_cache_hit_skips_build_no_network():
    """Image dengan hash konten yang sama sudah ada → `docker image inspect`
    sukses, TIDAK ADA panggilan `docker build` kedua (tanpa network sama sekali)."""
    calls = []

    async def _fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _fake_proc(returncode=0)

    sandbox = DockerSandbox()
    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await sandbox.build_project_image("requests==2.31.0\n")

    assert result["ok"] is True
    assert result["cached"] is True
    assert result["image"].startswith("openclawn-sandbox-proj:")
    assert len(calls) == 1, "cache hit tidak boleh memicu docker build"
    assert calls[0][:3] == ["docker", "image", "inspect"]


@pytest.mark.asyncio
async def test_build_project_image_cache_miss_builds_without_network_none():
    """Cache miss → docker build dipanggil TANPA --network none (satu-satunya
    invocation di modul ini yang sengaja network-nya terbuka)."""
    calls = []

    async def _fake_exec(*args, **kwargs):
        calls.append(list(args))
        if args[:3] == ("docker", "image", "inspect"):
            return _fake_proc(returncode=1)  # image belum ada
        return _fake_proc(returncode=0)

    sandbox = DockerSandbox()
    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await sandbox.build_project_image("requests==2.31.0\n")

    assert result["ok"] is True
    assert result["cached"] is False
    assert len(calls) == 2
    build_argv = calls[1]
    assert build_argv[:2] == ["docker", "build"]
    assert "--network" not in build_argv, "docker build TIDAK boleh dibatasi network di sini"


@pytest.mark.asyncio
async def test_build_project_image_same_content_yields_same_tag():
    async def _fake_exec(*args, **kwargs):
        return _fake_proc(returncode=0)

    sandbox = DockerSandbox()
    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        r1 = await sandbox.build_project_image("requests==2.31.0\n")
        r2 = await sandbox.build_project_image("requests==2.31.0\n")
        r3 = await sandbox.build_project_image("flask==3.0.0\n")

    assert r1["image"] == r2["image"]
    assert r1["image"] != r3["image"]


@pytest.mark.asyncio
async def test_build_project_image_build_failure_returns_error_dict():
    async def _fake_exec(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return _fake_proc(returncode=1)
        return _fake_proc(returncode=1, stderr=b"ERROR: No matching distribution found")

    sandbox = DockerSandbox()
    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await sandbox.build_project_image("no-such-package-xyz==999\n")

    assert result["ok"] is False
    assert result["image"] is None
    assert "No matching distribution" in result["log_tail"]


@pytest.mark.asyncio
async def test_build_project_image_timeout_kills_process():
    inspect_proc = _fake_proc(returncode=1)
    build_proc = _fake_proc(returncode=0)
    build_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

    async def _fake_exec(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return inspect_proc
        return build_proc

    sandbox = DockerSandbox()
    with patch("tools.sandbox.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        result = await sandbox.build_project_image("requests==2.31.0\n")

    assert result["ok"] is False
    assert "timeout" in result["error"].lower()
    build_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_build_project_image_fails_safe_when_docker_absent():
    with patch(
        "tools.sandbox.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("docker not found"),
    ):
        with pytest.raises(SandboxUnavailable):
            await DockerSandbox().build_project_image("requests==2.31.0\n")


# ── BuildSandboxImageTool ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_sandbox_image_tool_missing_file_errors(db, tmp_path, monkeypatch):
    _set_workspace(monkeypatch, tmp_path)
    tool = BuildSandboxImageTool()
    result = await tool.execute({"_session_id": "s1"}, vault=None, db=db)
    assert "error" in result
    assert "tidak ditemukan" in result["error"]


@pytest.mark.asyncio
async def test_build_sandbox_image_tool_rejects_invalid_requirements_without_calling_docker(
    tmp_path, monkeypatch
):
    _set_workspace(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("-e git+https://evil.example.com/x.git\n")
    tool = BuildSandboxImageTool()
    tool.sandbox.build_project_image = AsyncMock()

    result = await tool.execute({"_session_id": "s1"}, vault=None, db=None)

    assert "error" in result
    tool.sandbox.build_project_image.assert_not_called()


@pytest.mark.asyncio
async def test_build_sandbox_image_tool_success_persists_image_for_session(
    db, tmp_path, monkeypatch
):
    _set_workspace(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    tool = BuildSandboxImageTool()
    tool.sandbox.build_project_image = AsyncMock(
        return_value={
            "ok": True,
            "image": "openclawn-sandbox-proj:deadbeef1234",
            "cached": False,
            "error": None,
            "log_tail": "",
        }
    )

    result = await tool.execute({"_session_id": "sess-build"}, vault=None, db=db)

    assert result["ok"] is True
    assert await SessionSandboxImageStore(db).get("sess-build") == (
        "openclawn-sandbox-proj:deadbeef1234"
    )


@pytest.mark.asyncio
async def test_build_sandbox_image_tool_docker_unavailable_returns_error_not_raise(
    tmp_path, monkeypatch
):
    _set_workspace(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    tool = BuildSandboxImageTool()
    tool.sandbox.build_project_image = AsyncMock(
        side_effect=SandboxUnavailable("Docker tidak tersedia")
    )

    result = await tool.execute({"_session_id": "s1"}, vault=None, db=None)
    assert "error" in result


@pytest.mark.asyncio
async def test_build_sandbox_image_tool_missing_session_id_still_builds_but_does_not_persist(
    db, tmp_path, monkeypatch
):
    """`_session_id` absen (panggilan langsung tanpa konteks AgentLoop) → build
    tetap jalan (tool tak perlu tahu sesi untuk membangun image), tapi TIDAK
    menulis ke session_sandbox_image (tak ada sesi untuk diasosiasikan)."""
    _set_workspace(monkeypatch, tmp_path)
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    tool = BuildSandboxImageTool()
    tool.sandbox.build_project_image = AsyncMock(
        return_value={
            "ok": True,
            "image": "openclawn-sandbox-proj:nosession",
            "cached": False,
            "error": None,
            "log_tail": "",
        }
    )

    result = await tool.execute({}, vault=None, db=db)
    assert result["ok"] is True
    assert await SessionSandboxImageStore(db).get("") is None


# ── Integrasi AgentLoop: image terpakai otomatis lintas turn ──────────────────


@pytest.mark.asyncio
async def test_sandbox_image_persists_to_next_agentloop(db, tmp_path):
    """Turn 1: build_sandbox_image sukses. Turn 2 (AgentLoop BARU, sesi sama):
    code_run otomatis memakai image proyek TANPA tool lain dipanggil lagi."""
    sid = "sess-sandbox-image"
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    await SessionSandboxImageStore(db).set(sid, "openclawn-sandbox-proj:persisted123")

    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        captured["image"] = CURRENT_SANDBOX_IMAGE.get()
        yield LLMChunk(type="text", text="ok")

    agent = AgentLoop(
        AgentConfig(role="dev", session_id=sid, workspace_override=str(tmp_path)), db=db
    )
    agent.llm.stream_with_fallback = fake_stream
    _ = [ev async for ev in agent.run("halo")]

    assert captured["image"] == "openclawn-sandbox-proj:persisted123"


@pytest.mark.asyncio
async def test_no_sandbox_image_leaves_contextvar_unset(db, tmp_path):
    """Sesi yang tak pernah build_sandbox_image → CURRENT_SANDBOX_IMAGE tetap
    None sepanjang turn, code_run/shell_run jatuh ke SANDBOX_IMAGE dasar
    (perilaku lama, tak berubah)."""
    sid = "sess-no-image"
    captured = {}

    async def fake_stream(provider, model, messages, tools=None, max_tokens=4096):
        captured["image"] = CURRENT_SANDBOX_IMAGE.get()
        yield LLMChunk(type="text", text="ok")

    agent = AgentLoop(
        AgentConfig(role="dev", session_id=sid, workspace_override=str(tmp_path)), db=db
    )
    agent.llm.stream_with_fallback = fake_stream
    _ = [ev async for ev in agent.run("halo")]

    assert captured["image"] is None


def test_build_sandbox_image_registered_and_requires_approval():
    from tools import TOOL_REGISTRY

    assert "build_sandbox_image" in TOOL_REGISTRY
    assert TOOL_REGISTRY["build_sandbox_image"].requires_approval is True
    assert "build_sandbox_image" in _TRUST_MODE_EXEMPT
