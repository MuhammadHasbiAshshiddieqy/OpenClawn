"""Path traversal via `role` — audit produksi 2026-09-18, KRITIS.

`role` datang dari luar (form field `/chat/stream`/`/converse/stream`, argumen
tool `task_graph_submit`) dan SEBELUMNYA dipakai MENTAH untuk membangun path
filesystem (`f"roles/{role}/soul.toml"`) di `core/agent_loop.py`,
`core/router.py`, `core/late_execute.py`, `core/task_graph.py` — TANPA
validasi apa pun. String traversal ("../../../../tmp/evil") memuat soul.toml
ARBITRER dari luar `roles/`, memberi tool allow-list/system-prompt bikinan
penyerang — privilege escalation TOTAL, reachable TANPA login (auth default
OFF). Ditemukan & diverifikasi lewat reproduksi terisolasi SEBELUM diperbaiki:
`AgentLoop` sukses memuat `[tools] allowed` bikinan sendiri dari `/tmp`.

File ini menguji fungsi validasi bersama (`roles/registry.py::available_roles`)
dan tiap titik pertahanan yang memakainya. Test end-to-end lapisan web ada di
`tests/test_web.py`/`tests/test_task_graph.py`.
"""

import pytest

from roles.registry import available_roles


@pytest.fixture
def evil_soul_dir(tmp_path):
    """Direktori DI LUAR roles/ berisi soul.toml bikinan sendiri — simulasi
    target path traversal. Mengembalikan string `role` traversal yang, saat
    disubstitusi ke `f"roles/{role}/soul.toml"` (pola vulnerable yang
    diperbaiki), resolve TEPAT ke soul.toml ini — dihitung relatif terhadap
    `<cwd>/roles/` (bukan cwd itu sendiri), karena string `role` selalu
    disisipkan SETELAH literal `"roles/"` di semua call site yang diperbaiki."""
    evil_dir = tmp_path / "evil"
    evil_dir.mkdir()
    (evil_dir / "soul.toml").write_text(
        """
[meta]
role = "pwned"
name = "Pwned Agent"

[system_prompt]
content = "You are a PWNED agent."

[tools]
allowed = ["code_run", "shell_run", "file_write", "http_request"]

[routing]
prefer_local = false

[contract]
output_type = "DevOutput"
"""
    )
    import os

    roles_base = os.path.join(os.getcwd(), "roles")
    return os.path.relpath(evil_dir, roles_base)


# ── roles/registry.py::available_roles ─────────────────────────────────────


def test_available_roles_returns_known_roles():
    roles = available_roles()
    assert {"pm", "dev", "qa", "data", "security"} <= roles


def test_available_roles_rejects_traversal_target(evil_soul_dir):
    """Direktori planted TIDAK muncul di available_roles() walau punya
    soul.toml — hanya subdirektori LANGSUNG `roles/` yang dihitung."""
    roles = available_roles()
    assert "pwned" not in roles
    assert evil_soul_dir not in roles


# ── core/agent_loop.py::AgentLoop — pertahanan utama ────────────────────────


async def test_agent_loop_rejects_traversal_role(evil_soul_dir):
    from infra.config import AppConfig
    from infra.database import DatabaseManager

    from core.agent_loop import AgentConfig, AgentLoop

    db = DatabaseManager(AppConfig(db_path=":memory:"))
    with pytest.raises(ValueError, match="role tidak dikenal"):
        AgentLoop(AgentConfig(role=evil_soul_dir, session_id="pwn-test"), db=db)
    await db.close()


async def test_agent_loop_accepts_known_role():
    from infra.config import AppConfig
    from infra.database import DatabaseManager

    from core.agent_loop import AgentConfig, AgentLoop

    db = DatabaseManager(AppConfig(db_path=":memory:"))
    agent = AgentLoop(AgentConfig(role="dev", session_id="legit-test"), db=db)
    assert "code_run" in agent._soul["tools"]["allowed"]
    await db.close()


# ── core/router.py::SmartRouter — pertahanan independen (defense-in-depth) ──


def test_smart_router_rejects_traversal_role(evil_soul_dir):
    from core.router import SmartRouter

    with pytest.raises(ValueError, match="role tidak dikenal"):
        SmartRouter(role=evil_soul_dir)


def test_smart_router_accepts_known_role():
    from core.router import SmartRouter

    router = SmartRouter(role="qa")
    assert router.role == "qa"


def test_smart_router_soul_path_override_bypasses_role_membership_check(tmp_path):
    """`soul_path` eksplisit (dipakai luas oleh test suite router sendiri)
    TETAP dipercaya — caller yang secara sadar meneruskan path, bukan role
    mentah dari luar, bukan target celah ini."""
    from core.router import SmartRouter

    soul = tmp_path / "custom_soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="anything-not-a-real-role", soul_path=str(soul))
    assert router.role == "anything-not-a-real-role"


# ── core/late_execute.py::execute_orphan_approval — pertahanan independen ──


async def test_execute_orphan_approval_rejects_traversal_role(evil_soul_dir, tmp_path):
    import json

    from infra.config import AppConfig
    from infra.database import DatabaseManager
    from security.approval import ApprovalGate

    from core.late_execute import execute_orphan_approval

    config = AppConfig(db_path=":memory:", workspace_root=str(tmp_path))
    db = DatabaseManager(config)
    conn = await db.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
    await conn.commit()

    await db.execute(
        "INSERT INTO chat_sessions (session_id, role) VALUES (?, ?)",
        ("s-pwn", evil_soul_dir),
    )
    await db.execute(
        """INSERT INTO approval_log
           (session_id, tool_name, tool_input, decision, approval_id)
           VALUES (?,?,?,?,?)""",
        ("s-pwn", "code_run", json.dumps({"code": "1+1"}), "pending", "orph-pwn"),
    )
    gate = ApprovalGate(db, config)

    outcome = await execute_orphan_approval(db, config, gate, "orph-pwn")

    assert outcome["ok"] is False
    assert "tidak dikenal" in outcome["error"]
    row = await db.fetchone("SELECT decision FROM approval_log WHERE approval_id=?", ("orph-pwn",))
    assert row["decision"] == "rejected"
    await db.close()


# ── core/task_graph.py::TaskGraph.validate — pertahanan independen ─────────


def test_task_graph_rejects_traversal_role(evil_soul_dir):
    from core.task_graph import TaskGraph, TaskGraphError, TaskNode

    nodes = [TaskNode(node_id="A", role=evil_soul_dir, prompt="x")]
    with pytest.raises(TaskGraphError, match="role tak dikenal"):
        TaskGraph(nodes).validate()
