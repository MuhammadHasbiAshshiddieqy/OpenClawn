"""Test untuk core/task_graph.py — model DAG murni (§ Task Graph, Fase 1).

Tanpa I/O/DB/LLM — murni algoritma graph (topological ready-set, deteksi
siklus, validasi). Role divalidasi terhadap folder `roles/` sungguhan yang ada
di repo (pm/qa/dev/data/security) — konsisten `infra/manifest.py`.
"""

import pytest

from core.task_graph import TaskGraph, TaskGraphError, TaskNode


def test_valid_diamond_dag_accepted():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
        TaskNode(node_id="C", role="dev", prompt="c", depends_on=["A"]),
        TaskNode(node_id="D", role="dev", prompt="d", depends_on=["B", "C"]),
    ]
    graph = TaskGraph(nodes)
    graph.validate()  # tak boleh raise


def test_empty_graph_rejected():
    with pytest.raises(TaskGraphError):
        TaskGraph([]).validate()


def test_duplicate_node_id_rejected():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="A", role="dev", prompt="a-duplikat"),
    ]
    with pytest.raises(TaskGraphError, match="duplikat"):
        TaskGraph(nodes).validate()


def test_unknown_depends_on_target_rejected():
    nodes = [TaskNode(node_id="A", role="dev", prompt="a", depends_on=["ghost"])]
    with pytest.raises(TaskGraphError, match="tak dikenal"):
        TaskGraph(nodes).validate()


def test_self_dependency_rejected():
    nodes = [TaskNode(node_id="A", role="dev", prompt="a", depends_on=["A"])]
    with pytest.raises(TaskGraphError, match="dirinya sendiri"):
        TaskGraph(nodes).validate()


def test_unknown_role_rejected():
    nodes = [TaskNode(node_id="A", role="not-a-real-role", prompt="a")]
    with pytest.raises(TaskGraphError, match="role tak dikenal"):
        TaskGraph(nodes).validate()


def test_simple_two_node_cycle_detected():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a", depends_on=["B"]),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
    ]
    with pytest.raises(TaskGraphError, match="siklus"):
        TaskGraph(nodes).validate()


def test_longer_cycle_detected():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a", depends_on=["C"]),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
        TaskNode(node_id="C", role="dev", prompt="c", depends_on=["B"]),
    ]
    with pytest.raises(TaskGraphError, match="siklus"):
        TaskGraph(nodes).validate()


def test_ready_nodes_on_diamond():
    """`ready_nodes(completed)` mengasumsikan `completed` KONSISTEN dengan
    `node.status` (pola nyata `core/task_executor.py`: begitu satu node
    ditandai 'completed', id-nya JUGA masuk ke set `completed`) — simulasikan
    itu persis di sini, bukan cuma memberi id tanpa mengubah status node."""
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
        TaskNode(node_id="C", role="dev", prompt="c", depends_on=["A"]),
        TaskNode(node_id="D", role="dev", prompt="d", depends_on=["B", "C"]),
    ]
    graph = TaskGraph(nodes)

    assert {n.node_id for n in graph.ready_nodes(set())} == {"A"}

    graph.nodes["A"].status = "completed"
    assert {n.node_id for n in graph.ready_nodes({"A"})} == {"B", "C"}

    # D butuh KEDUANYA B dan C selesai — belum siap dengan salah satu saja.
    graph.nodes["B"].status = "completed"
    assert {n.node_id for n in graph.ready_nodes({"A", "B"})} == {"C"}

    graph.nodes["C"].status = "completed"
    assert {n.node_id for n in graph.ready_nodes({"A", "B", "C"})} == {"D"}


def test_ready_nodes_excludes_non_pending():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a", status="completed"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
    ]
    graph = TaskGraph(nodes)
    # A sudah completed (bukan pending) — tak boleh muncul lagi di ready_nodes.
    assert {n.node_id for n in graph.ready_nodes({"A"})} == {"B"}


def test_transitive_dependents_diamond():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a"),
        TaskNode(node_id="B", role="dev", prompt="b", depends_on=["A"]),
        TaskNode(node_id="C", role="dev", prompt="c", depends_on=["A"]),
        TaskNode(node_id="D", role="dev", prompt="d", depends_on=["B", "C"]),
        TaskNode(node_id="E", role="dev", prompt="e"),  # independen, bukan dependent A
    ]
    graph = TaskGraph(nodes)
    assert graph.transitive_dependents("A") == {"B", "C", "D"}


def test_is_terminal():
    nodes = [
        TaskNode(node_id="A", role="dev", prompt="a", status="completed"),
        TaskNode(node_id="B", role="dev", prompt="b", status="failed"),
    ]
    assert TaskGraph(nodes).is_terminal() is True

    nodes2 = [
        TaskNode(node_id="A", role="dev", prompt="a", status="completed"),
        TaskNode(node_id="B", role="dev", prompt="b", status="pending"),
    ]
    assert TaskGraph(nodes2).is_terminal() is False
