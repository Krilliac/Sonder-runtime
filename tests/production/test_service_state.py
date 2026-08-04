"""SPEC-2 section 4: process and dependency state machine."""
from __future__ import annotations

import pytest

from sonder_service_state import (
    DependencyState,
    InvalidTransition,
    ProcessState,
    ServiceStateTracker,
)

pytestmark = pytest.mark.unit


def _to_ready(tracker: ServiceStateTracker) -> None:
    tracker.transition(ProcessState.MIGRATING, "preflight passed")
    tracker.transition(ProcessState.READY, "dependencies healthy")


def test_happy_path_lifecycle():
    tracker = ServiceStateTracker()
    assert tracker.snapshot().process is ProcessState.STARTING
    _to_ready(tracker)
    assert tracker.snapshot().serving
    tracker.transition(ProcessState.DRAINING, "shutdown")
    tracker.transition(ProcessState.STOPPING, "drained")
    assert tracker.snapshot().process is ProcessState.STOPPING


def test_invalid_transitions_rejected():
    tracker = ServiceStateTracker()
    with pytest.raises(InvalidTransition):
        tracker.transition(ProcessState.READY)  # STARTING -> READY skips MIGRATING
    _to_ready(tracker)
    tracker.transition(ProcessState.DRAINING)
    with pytest.raises(InvalidTransition):
        tracker.transition(ProcessState.READY)  # DRAINING is one-way


def test_optional_dependency_loss_degrades_and_recovers():
    tracker = ServiceStateTracker()
    tracker.register_dependency("npu", required=False)
    _to_ready(tracker)
    snap = tracker.set_dependency("npu", DependencyState.UNAVAILABLE, "gone")
    assert snap.process is ProcessState.DEGRADED
    ready, _ = tracker.ready_for_traffic()
    assert ready  # degraded still serves
    snap = tracker.set_dependency("npu", DependencyState.READY)
    assert snap.process is ProcessState.READY


def test_required_dependency_loss_blocks_readiness_not_liveness():
    tracker = ServiceStateTracker()
    tracker.register_dependency("ollama", required=True)
    _to_ready(tracker)
    tracker.set_dependency("ollama", DependencyState.UNAVAILABLE, "refused")
    ready, reason = tracker.ready_for_traffic()
    assert not ready
    assert "ollama" in reason
    # The process itself is still serving (liveness would pass).
    assert tracker.snapshot().serving


def test_fail_is_terminal():
    tracker = ServiceStateTracker()
    _to_ready(tracker)
    tracker.fail("unrecoverable invariant")
    assert tracker.snapshot().process is ProcessState.FAILED
    with pytest.raises(InvalidTransition):
        tracker.transition(ProcessState.READY)


def test_listener_sees_transitions():
    tracker = ServiceStateTracker()
    seen = []
    tracker.add_listener(lambda snap: seen.append(snap.process))
    _to_ready(tracker)
    assert seen == [ProcessState.MIGRATING, ProcessState.READY]
