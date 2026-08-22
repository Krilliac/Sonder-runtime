from __future__ import annotations

import threading

from sonder_runtime.application.cancellation_tree import CancellationTree
from sonder_runtime.domain.cancellation_tree import CancellationStatus


def test_parent_cancellation_propagates_to_existing_and_late_children():
    tree = CancellationTree()
    child = tree.create_child(node_id="child")
    grandchild = tree.create_child("child", node_id="grandchild")

    assert tree.cancel(reason="user stopped turn") is True
    assert child.cancelled and grandchild.cancelled
    assert child.reason == "user stopped turn"
    assert tree.cancel(reason="different reason") is False

    late = tree.create_child(node_id="late")
    assert late.cancelled
    assert late.reason == "user stopped turn"


def test_cancel_is_idempotent_and_first_reason_wins():
    tree = CancellationTree()
    node = tree.create_child(node_id="work")

    assert node.cancel(reason="timeout") is True
    assert node.cancel(reason="shutdown") is False
    assert node.snapshot().status is CancellationStatus.CANCEL_REQUESTED
    assert node.reason == "timeout"


def test_join_waits_for_all_descendant_leases_and_reaches_quiescence():
    tree = CancellationTree()
    child = tree.create_child(node_id="tool")
    lease = child.acquire()
    tree.cancel(reason="stop")

    result: list[bool] = []
    waiter = threading.Thread(target=lambda: result.append(tree.join(timeout=1)))
    waiter.start()
    assert tree.status().status is CancellationStatus.CANCEL_REQUESTED
    assert tree.join(timeout=0.01) is False
    lease.release()
    waiter.join(timeout=1)

    assert result == [True]
    assert tree.status().status is CancellationStatus.QUIESCENT
    assert child.status is CancellationStatus.QUIESCENT


def test_lease_context_releases_even_when_work_raises():
    tree = CancellationTree()
    node = tree.create_child(node_id="stream")
    with node.acquire():
        node.cancel(reason="client disconnected")
        assert node.status is CancellationStatus.CANCEL_REQUESTED
    assert node.join(timeout=0) is True
