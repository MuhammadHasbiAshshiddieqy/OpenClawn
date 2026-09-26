"""Tool `task_graph_submit`: agent memecah satu goal jadi DAG subtask EKSPLISIT
(§ Task Graph, dari IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 1+2 —
owner memilih submission eksplisit, BUKAN auto-decompose LLM, untuk versi ini).

Subtask independen (tak saling `depends_on`) dijalankan PARALEL, masing-masing
sebagai `AgentLoop` terpisah dengan sesi sendiri (context hygiene — tak ada
transkrip mentah yang dibagi antar-subtask). Tool ini BLOCKS sampai seluruh
graph selesai (atau timeout per-node/percobaan habis) — lihat
`core/task_executor.py` untuk kenapa ini desain yang benar untuk v1 (belum ada
endpoint polling/observability terpisah, itu fase lanjutan yang ditunda).

`requires_approval=False`: tool ini SENDIRI hanya mengorkestrasi turn agent
lain, tidak melakukan aksi destruktif apa pun secara langsung — aksi destruktif
yang subtask-nya coba lakukan tetap digerbangi individual (queue_proposal,
karena tiap subtask SELALU `autopilot=True`, lihat core/task_executor.py).
"""

import uuid

from core.task_graph import TaskGraph, TaskGraphError, TaskNode
from infra.config import CONFIG
from security.approval import ApprovalGate
from tools.base import Tool

# `TaskGraphExecutor`/`AgentLoop` diimpor LOKAL di dalam fungsi (bukan di sini) —
# `core.task_executor`/`core.agent_loop` saling mengimpor lewat rantai
# `core.agent_loop` -> `tools` (TOOL_REGISTRY, diimpor SEBELUM class AgentLoop
# didefinisikan) -> modul tool ini. Impor level-modul di sini akan memicu
# ImportError "partially initialized module" — impor lokal aman karena baru
# dieksekusi saat `execute()` dipanggil, jauh setelah semua modul selesai dimuat.

MAX_NODES = 20


class TaskGraphSubmitTool(Tool):
    name = "task_graph_submit"
    requires_approval = False

    # Audit 2026-09-25: budget graph penuh, bukan tool_timeout_sec (40s).
    timeout_sec = CONFIG.task_graph_timeout_sec

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        session_id = input_data.get("_session_id")
        if not session_id:
            return {"error": "task_graph_submit butuh konteks sesi (internal)"}
        if db is None:
            return {"error": "task_graph_submit butuh database"}

        raw_nodes = input_data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            return {"error": "nodes wajib berupa list subtask (minimal satu)"}
        if len(raw_nodes) > MAX_NODES:
            return {"error": f"terlalu banyak subtask (maks {MAX_NODES})"}

        nodes = []
        for i, item in enumerate(raw_nodes):
            if not isinstance(item, dict):
                return {"error": f"subtask ke-{i} harus objek {{node_id, role, prompt}}"}
            node_id = str(item.get("node_id", "")).strip()
            role = str(item.get("role", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
            depends_on = item.get("depends_on") or []
            if not node_id or not role or not prompt:
                return {"error": f"subtask ke-{i} butuh node_id, role, dan prompt"}
            if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
                return {"error": f"subtask '{node_id}': depends_on harus list string"}
            nodes.append(TaskNode(node_id=node_id, role=role, prompt=prompt, depends_on=depends_on))

        try:
            # `TaskGraph.__init__` sendiri bisa raise (node_id duplikat) selain
            # `.validate()` (depends_on tak dikenal, role tak dikenal, siklus) —
            # keduanya harus tertangkap di sini.
            graph = TaskGraph(nodes)
            graph.validate()
        except TaskGraphError as e:
            # Fail-closed: TIDAK ADA baris DB ditulis, TIDAK ADA subtask dimulai
            # bila graph malformed/cyclic — semua-atau-tidak-sama-sekali.
            return {"error": str(e)}

        task_id = uuid.uuid4().hex
        user_id = input_data.get("_user_id")
        owner_user_id = user_id if user_id and user_id != "default" else None
        goal = str(input_data.get("goal", "")).strip()

        from core.task_executor import TaskGraphExecutor

        executor = TaskGraphExecutor(
            db=db,
            config=CONFIG,
            agent_factory=lambda cfg: _build_agent(cfg, db),
        )
        return await executor.run(
            task_id=task_id,
            graph=graph,
            owner_user_id=owner_user_id,
            parent_session_id=session_id,
            goal=goal,
        )

    def schema(self) -> dict:
        return {
            "name": "task_graph_submit",
            "description": (
                "Pecah SATU goal jadi beberapa subtask EKSPLISIT dengan dependency, "
                "lalu jalankan — subtask independen (tak saling depends_on) berjalan "
                "PARALEL, masing-masing sebagai agent terpisah dengan konteks sendiri "
                "(bukan berbagi transkrip). Tool ini MENUNGGU sampai seluruh subtask "
                "selesai lalu mengembalikan ringkasan hasil tiap subtask. Pakai untuk "
                "goal yang benar-benar bisa dipecah jadi bagian independen — jangan "
                "pakai untuk tugas linear sederhana (pakai todo_write untuk itu)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Deskripsi ringkas goal (opsional)."},
                    "nodes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "node_id": {
                                    "type": "string",
                                    "description": "ID unik subtask ini dalam graph.",
                                },
                                "role": {
                                    "type": "string",
                                    "description": "Role yang menjalankan subtask (pm/dev/qa/data).",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Instruksi lengkap untuk subtask ini.",
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "node_id lain yang harus selesai dulu (opsional).",
                                },
                            },
                            "required": ["node_id", "role", "prompt"],
                        },
                    },
                },
                "required": ["nodes"],
            },
        }


def _build_agent(cfg, db):
    """Factory `AgentLoop` untuk satu subtask node — impor lokal untuk hindari
    siklus impor (`core.agent_loop` sudah mengimpor `tools/__init__.py` yang
    mengimpor modul tool ini)."""
    from core.agent_loop import AgentLoop

    return AgentLoop(cfg, db=db, config=CONFIG, approval=ApprovalGate(db, CONFIG))
