"""Model DAG subtask (§ Task Graph, dari IMPROVEMENT-Sandbox-Isolation-Parallelization.md,
Fase 1: DAG + eksekusi paralel — owner memilih submission EKSPLISIT, bukan
auto-decompose LLM, untuk versi ini).

Modul ini MURNI struktur data + algoritma graph — tanpa I/O, tanpa DB, tanpa LLM,
agar sepenuhnya bisa diuji terisolasi (CLAUDE.md §1.6, extractable). Eksekusi
sungguhan (menjalankan tiap node sebagai `AgentLoop`) ada di `core/task_executor.py`.

Validasi SELALU fail-closed: graph yang cyclic atau malformed ditolak SELURUHNYA
sebelum satu subtask pun mulai — bukan gagal di tengah jalan setelah sebagian
subtask sudah terlanjur jalan (§1, keamanan/kebenaran dulu).
"""

from dataclasses import dataclass, field
from pathlib import Path

VALID_NODE_STATUS = {"pending", "running", "completed", "failed", "blocked"}


class TaskGraphError(Exception):
    """Graph tidak valid — node_id duplikat, depends_on ke node tak dikenal,
    role tak dikenal, atau ada siklus. Dilempar SEBELUM eksekusi apa pun dimulai."""


@dataclass
class TaskNode:
    """Satu subtask dalam DAG. `depends_on` berisi `node_id` lain dalam graph
    YANG SAMA — subtask ini baru boleh jalan setelah semua itu `completed`."""

    node_id: str
    role: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    attempt_count: int = 0
    result_summary: str | None = None
    error: str | None = None


class TaskGraph:
    """Kumpulan `TaskNode` + algoritma graph murni (topological ready-set,
    deteksi siklus). Tidak menyimpan state eksekusi lintas proses — itu
    tanggung jawab `core/task_executor.py` (persist ke tabel `task_nodes`)."""

    def __init__(self, nodes: list[TaskNode], roles_dir: str = "roles"):
        # Deteksi duplikat DI SINI, sebelum masuk dict — `{n.node_id: n for n
        # in nodes}` akan diam-diam menimpa entry lama untuk node_id yang
        # sama, membuat validate() tak pernah melihat duplikatnya lagi.
        seen_ids: set[str] = set()
        for n in nodes:
            if n.node_id in seen_ids:
                raise TaskGraphError(f"node_id duplikat: '{n.node_id}'")
            seen_ids.add(n.node_id)
        self.nodes: dict[str, TaskNode] = {n.node_id: n for n in nodes}
        self._roles_dir = roles_dir

    def validate(self) -> None:
        """Cek depends_on ke node tak dikenal, role tak dikenal, dan siklus.
        Node_id duplikat sudah dicek di `__init__` (lihat komentar di sana).
        Raise `TaskGraphError` pada pelanggaran PERTAMA yang ditemukan —
        caller (tool) tidak boleh menyentuh DB sama sekali sebelum ini lolos
        bersih."""
        if not self.nodes:
            raise TaskGraphError("graph kosong — minimal satu subtask")

        for node_id, node in self.nodes.items():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise TaskGraphError(f"node '{node_id}' depends_on node tak dikenal: '{dep}'")
                if dep == node_id:
                    raise TaskGraphError(f"node '{node_id}' depends_on dirinya sendiri")
            # Role harus punya soul.toml — cek sama persis dengan
            # infra/manifest.py::apply_manifest (satu sumber kebenaran "role
            # dikenal" = folder roles/<role>/soul.toml ada), bukan registry baru.
            if not (Path(self._roles_dir) / node.role / "soul.toml").exists():
                raise TaskGraphError(f"node '{node_id}' pakai role tak dikenal: '{node.role}'")

        self.detect_cycle()

    def detect_cycle(self) -> None:
        """DFS dengan recursion-stack — raise `TaskGraphError` pada siklus apa pun."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self.nodes, WHITE)

        def visit(node_id: str, path: list[str]) -> None:
            color[node_id] = GRAY
            for dep in self.nodes[node_id].depends_on:
                if color[dep] == GRAY:
                    cycle = " -> ".join([*path, node_id, dep])
                    raise TaskGraphError(f"siklus terdeteksi: {cycle}")
                if color[dep] == WHITE:
                    visit(dep, [*path, node_id])
            color[node_id] = BLACK

        for node_id in self.nodes:
            if color[node_id] == WHITE:
                visit(node_id, [])

    def ready_nodes(self, completed: set[str]) -> list[TaskNode]:
        """Node `pending` yang SEMUA depends_on-nya sudah ada di `completed`."""
        return [
            n
            for n in self.nodes.values()
            if n.status == "pending" and set(n.depends_on) <= completed
        ]

    def transitive_dependents(self, node_id: str) -> set[str]:
        """Semua node yang (langsung atau tak langsung) depends_on `node_id` —
        dipakai executor menandai `blocked` saat satu node gagal permanen."""
        dependents: set[str] = set()
        changed = True
        while changed:
            changed = False
            for n in self.nodes.values():
                if n.node_id in dependents:
                    continue
                if node_id in n.depends_on or (n.depends_on and dependents & set(n.depends_on)):
                    dependents.add(n.node_id)
                    changed = True
        return dependents

    def is_terminal(self) -> bool:
        """True bila tak ada node `pending`/`running` tersisa — graph selesai
        (baik semua completed, atau sisanya failed/blocked permanen)."""
        return all(n.status not in ("pending", "running") for n in self.nodes.values())
