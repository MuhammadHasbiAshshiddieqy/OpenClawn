"""Eksekusi DAG subtask (§ Task Graph, Fase 2: concurrency engine + fault
containment). Lihat `core/task_graph.py` untuk model data murni; modul ini
menjalankannya sungguhan — tiap node = satu `AgentLoop.run()` independen.

Penjadwalan EVENT-DRIVEN (bukan "wave" tetap): begitu satu node selesai,
langsung cek ulang node mana yang baru siap (dependency-nya sudah tuntas),
alih-alih menunggu SELURUH batch/wave lain — node cepat tak perlu menunggu
sibling lambat yang tak jadi dependency-nya.

Fault containment TERSTRUKTUR di titik eksekusi (`_run_node`), bukan hanya di
titik agregasi (`asyncio.gather(..., return_exceptions=True)`): satu node yang
meledak tak pernah menjalar ke `asyncio.wait` sama sekali, jadi tak mungkin
membatalkan/merusak sibling yang sedang jalan bersamaan.

`autopilot=True` WAJIB untuk tiap subtask (bukan opsional) — pelajaran langsung
dari bug `scripts/run_evals.py` sesi ini: sub-AgentLoop di sini TIDAK punya
listener SSE/UI apa pun yang mengawasi session-nya. Tool yang butuh approval
manusia lewat `ApprovalGate.request()` biasa akan menggantung sampai timeout
tanpa ada yang pernah melihat kartu approval-nya. Dengan autopilot=True, tool
semacam itu diantri sebagai proposal (`ApprovalGate.queue_proposal`, sudah
terpasang di `AgentLoop._execute_tool`) — tetap tercatat, bisa ditinjau lewat
GET /approvals nanti, TIDAK menggantung graph.
"""

import asyncio
import json
from collections.abc import Callable

from core.agent_loop import AgentConfig, AgentLoop
from core.task_graph import TaskGraph, TaskNode
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.logging import log

AgentFactory = Callable[[AgentConfig], AgentLoop]

# Batas panjang result_summary yang disimpan/dikembalikan — token-first §1.4,
# subtask lain (dependent) hanya butuh RINGKASAN, bukan transkrip penuh.
MAX_RESULT_SUMMARY_CHARS = 2000


class TaskGraphExecutor:
    """Jalankan satu `TaskGraph` sampai tuntas (semua node completed/failed/blocked).

    `agent_factory` dipanggil sekali per node PER PERCOBAAN (retry membuat
    AgentLoop baru, bukan reuse instance lama) — pola sama tiap turn AgentLoop
    dibuat baru di web layer, tak ada state instance yang perlu dipertahankan
    lintas percobaan.
    """

    def __init__(self, db: DatabaseManager, config: AppConfig, agent_factory: AgentFactory):
        self.db = db
        self.config = config
        self.agent_factory = agent_factory

    async def run(
        self,
        task_id: str,
        graph: TaskGraph,
        owner_user_id: str | None,
        parent_session_id: str,
        goal: str = "",
    ) -> dict:
        """Persist graph, jalankan sampai tuntas, return ringkasan.

        Return `{"status": "completed"|"partial"|"failed", "nodes": {node_id:
        {"status", "result_summary", "error"}}}`. `graph.validate()` HARUS
        sudah dipanggil caller (tool) SEBELUM ini — executor tidak validasi
        ulang, hanya mengeksekusi apa yang diberikan.
        """
        await self.db.execute(
            """INSERT INTO task_graphs (id, goal, owner_user_id, session_id, status)
               VALUES (?, ?, ?, ?, 'running')""",
            (task_id, goal, owner_user_id, parent_session_id),
        )
        for node in graph.nodes.values():
            await self.db.execute(
                """INSERT INTO task_nodes
                   (task_id, node_id, role, prompt, depends_on_json, status, session_id)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    task_id,
                    node.node_id,
                    node.role,
                    node.prompt,
                    json.dumps(node.depends_on),
                    f"{task_id}:{node.node_id}",
                ),
            )

        completed: set[str] = set()
        running: dict[str, asyncio.Task] = {}

        while not graph.is_terminal():
            ready = [n for n in graph.ready_nodes(completed) if n.node_id not in running]
            for node in ready:
                if len(running) >= self.config.task_graph_max_concurrency:
                    break
                node.status = "running"
                running[node.node_id] = asyncio.create_task(self._run_node(task_id, node))

            if not running:
                # Tak ada yang siap DAN tak ada yang jalan → sisanya (pending)
                # tak akan pernah siap lagi (dependency-nya failed/blocked di
                # cabang lain) — tandai blocked lalu selesai, jangan berputar.
                for n in graph.nodes.values():
                    if n.status == "pending":
                        n.status = "blocked"
                        await self._persist_node(task_id, n)
                break

            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            finished_ids = [nid for nid, t in running.items() if t in done]
            for node_id in finished_ids:
                del running[node_id]
                node = graph.nodes[node_id]
                if node.status == "completed":
                    completed.add(node_id)
                elif node.status == "failed":
                    for dep_id in graph.transitive_dependents(node_id):
                        dep = graph.nodes[dep_id]
                        if dep.status == "pending":
                            dep.status = "blocked"
                            await self._persist_node(task_id, dep)

        statuses = {n.status for n in graph.nodes.values()}
        if statuses == {"completed"}:
            graph_status = "completed"
        elif "completed" in statuses:
            graph_status = "partial"
        else:
            graph_status = "failed"

        await self.db.execute(
            "UPDATE task_graphs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (graph_status, task_id),
        )

        return {
            "status": graph_status,
            "nodes": {
                n.node_id: {
                    "status": n.status,
                    "result_summary": n.result_summary,
                    "error": n.error,
                }
                for n in graph.nodes.values()
            },
        }

    async def _run_node(self, task_id: str, node: TaskNode) -> None:
        """Jalankan satu node sampai sukses atau kehabisan percobaan. Tak pernah
        melempar exception ke caller — kegagalan tercatat di `node.error`/`status`,
        bukan menjalar ke `asyncio.wait` (itulah yang membuat fault containment
        struktural, bukan cuma `return_exceptions=True` di titik agregasi)."""
        await self._persist_node(task_id, node)
        max_attempts = self.config.task_graph_max_node_attempts
        for attempt in range(1, max_attempts + 1):
            node.attempt_count = attempt
            try:
                child_cfg = AgentConfig(
                    role=node.role,
                    session_id=f"{task_id}:{node.node_id}",
                    autopilot=True,
                    persist_history=False,
                    task_id=task_id,
                    node_id=node.node_id,
                )
                agent = self.agent_factory(child_cfg)
                async with asyncio.timeout(self.config.task_graph_node_timeout_sec):
                    async for _ in agent.run(node.prompt):
                        pass  # tak ada listener SSE untuk subtask — lihat docstring modul
                turn = agent.history[-1] if agent.history else None
                node.result_summary = (turn.content if turn else "")[:MAX_RESULT_SUMMARY_CHARS]
                node.error = None
                node.status = "completed"
                await self._persist_node(task_id, node)
                return
            except Exception as exc:  # noqa: BLE001 — subtask pihak ketiga, harus gagal anggun
                node.error = str(exc)[:MAX_RESULT_SUMMARY_CHARS]
                log.warning(
                    "task_node_attempt_failed",
                    task_id=task_id,
                    node_id=node.node_id,
                    attempt=attempt,
                    error=node.error,
                )
                if attempt < max_attempts:
                    backoff = self.config.task_graph_retry_backoff_sec * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff)
                    continue
                node.status = "failed"
                await self._persist_node(task_id, node)
                return

    async def _persist_node(self, task_id: str, node: TaskNode) -> None:
        await self.db.execute(
            """UPDATE task_nodes SET status=?, attempt_count=?, result_summary=?, error=?,
               updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND node_id=?""",
            (
                node.status,
                node.attempt_count,
                node.result_summary,
                node.error,
                task_id,
                node.node_id,
            ),
        )
