import threading
from pathlib import Path

import sonder_service_state
import sonder_runtime.platform.service_state as packaged_state
from sonder_runtime.platform.service_state import (
    DependencyState,
    ProcessState,
    ServiceStateTracker,
)


def _ready(tracker: ServiceStateTracker) -> None:
    tracker.transition(ProcessState.MIGRATING)
    tracker.transition(ProcessState.READY)


def test_root_service_state_is_the_packaged_module_identity():
    assert sonder_service_state is packaged_state
    assert Path(packaged_state.__file__).match(
        "sonder_runtime/platform/service_state.py"
    )
    for name in (
        "DependencyState",
        "InvalidTransition",
        "ProcessState",
        "ServiceStateTracker",
        "StateSnapshot",
    ):
        assert getattr(sonder_service_state, name) is getattr(packaged_state, name)


def test_root_service_state_has_no_implementation_declarations():
    source = Path("sonder_service_state.py").read_text(encoding="utf-8")
    assert "class ServiceStateTracker" not in source
    assert "def transition" not in source


def test_state_tracker_preserves_monkeypatchable_module_policy(monkeypatch):
    replacement = frozenset({ProcessState.MIGRATING})
    monkeypatch.setitem(packaged_state._ALLOWED, ProcessState.STARTING, replacement)
    tracker = ServiceStateTracker()
    tracker.transition(ProcessState.MIGRATING)


def test_state_tracker_is_safe_under_concurrent_readiness_and_dependency_updates():
    tracker = ServiceStateTracker()
    _ready(tracker)
    tracker.register_dependency("optional", required=False)
    failures = []

    def mutate() -> None:
        try:
            for index in range(250):
                state = (
                    DependencyState.UNAVAILABLE
                    if index % 2
                    else DependencyState.READY
                )
                tracker.set_dependency("optional", state)
        except Exception as exc:  # pragma: no cover - failure diagnostic
            failures.append(exc)

    def inspect() -> None:
        try:
            for _ in range(500):
                snapshot = tracker.snapshot()
                assert snapshot.process in {
                    ProcessState.READY,
                    ProcessState.DEGRADED,
                }
                tracker.ready_for_traffic()
        except Exception as exc:  # pragma: no cover - failure diagnostic
            failures.append(exc)

    threads = [threading.Thread(target=mutate) for _ in range(2)]
    threads += [threading.Thread(target=inspect) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert failures == []
