"""SPEC-2 section 12: graceful drain with deadline and interruption."""
from __future__ import annotations

import threading

import pytest

from sonder_service_state import ProcessState, ServiceStateTracker
from sonder_shutdown import ShutdownCoordinator

pytestmark = pytest.mark.unit


def _ready_tracker() -> ServiceStateTracker:
    tracker = ServiceStateTracker()
    tracker.transition(ProcessState.MIGRATING)
    tracker.transition(ProcessState.READY)
    return tracker


def test_clean_drain_when_idle():
    tracker = _ready_tracker()
    flushed = []
    coordinator = ShutdownCoordinator(tracker, drain_deadline_seconds=5)
    coordinator.add_flush_hook(lambda: flushed.append(1))
    assert coordinator.drain(reason="test") is True
    assert flushed == [1]
    assert tracker.snapshot().process is ProcessState.STOPPING


def test_drain_rejects_new_mutations_and_waits_for_active():
    tracker = _ready_tracker()
    coordinator = ShutdownCoordinator(tracker, drain_deadline_seconds=5)
    assert coordinator.begin_mutation() is True

    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(coordinator.drain(reason="test"))
    )
    thread.start()
    # Draining begins: new mutations are rejected while ours is active.
    deadline = threading.Event()
    for _ in range(100):
        if coordinator.draining:
            break
        deadline.wait(0.02)
    assert coordinator.begin_mutation() is False
    assert coordinator.cancellation.cancelled
    coordinator.end_mutation()
    thread.join(timeout=5)
    assert result == [True]
    assert tracker.snapshot().process is ProcessState.STOPPING


def test_deadline_expiry_marks_interrupted():
    tracker = _ready_tracker()
    interrupted = []
    coordinator = ShutdownCoordinator(tracker, drain_deadline_seconds=0.2)
    coordinator.add_interrupted_hook(lambda: interrupted.append(1))
    assert coordinator.begin_mutation() is True
    clean = coordinator.drain(reason="test")
    assert clean is False
    assert interrupted == [1]
    assert tracker.snapshot().process is ProcessState.STOPPING


def test_drain_is_idempotent():
    tracker = _ready_tracker()
    coordinator = ShutdownCoordinator(tracker, drain_deadline_seconds=1)
    assert coordinator.drain() is True
    assert coordinator.drain() is True


def test_concurrent_drain_runs_hooks_once():
    tracker = _ready_tracker()
    coordinator = ShutdownCoordinator(tracker, drain_deadline_seconds=1)
    real_event = coordinator._draining
    simultaneous_reads = threading.Barrier(2)

    class RaceWindowEvent:
        def is_set(self):
            # Without the coordinator lock, force both check-then-act callers
            # to observe the old value before either can set it.
            if not coordinator._lock.locked():
                simultaneous_reads.wait(1)
            return real_event.is_set()

        def set(self):
            real_event.set()

    coordinator._draining = RaceWindowEvent()
    flushed = []
    coordinator.add_flush_hook(lambda: flushed.append(1))
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(coordinator.drain()))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True]
    assert flushed == [1]
