from pathlib import Path

import sonder_service_state
import sonder_runtime.adapters.web.lifecycle as web_lifecycle
import sonder_runtime.application.lifecycle as application_lifecycle
import sonder_runtime.platform.service_state as packaged_state


def test_process_state_metric_projection_belongs_to_application_boundary():
    assert web_lifecycle._state_number is application_lifecycle.process_state_number
    assert sonder_service_state is packaged_state
    assert sonder_service_state.ServiceStateTracker is packaged_state.ServiceStateTracker


def test_application_projection_preserves_all_process_state_numbers():
    cases = []
    for expected_state in packaged_state.ProcessState:
        tracker = packaged_state.ServiceStateTracker()
        if expected_state is packaged_state.ProcessState.MIGRATING:
            tracker.transition(expected_state)
        elif expected_state is packaged_state.ProcessState.RECOVERY_REQUIRED:
            tracker.transition(packaged_state.ProcessState.MIGRATING)
            tracker.transition(expected_state)
        elif expected_state is packaged_state.ProcessState.READY:
            tracker.transition(packaged_state.ProcessState.MIGRATING)
            tracker.transition(expected_state)
        elif expected_state is packaged_state.ProcessState.DEGRADED:
            tracker.transition(packaged_state.ProcessState.MIGRATING)
            tracker.transition(packaged_state.ProcessState.READY)
            tracker.register_dependency("optional", required=False)
            tracker.set_dependency(
                "optional", packaged_state.DependencyState.UNAVAILABLE
            )
        elif expected_state is packaged_state.ProcessState.DRAINING:
            tracker.transition(packaged_state.ProcessState.MIGRATING)
            tracker.transition(packaged_state.ProcessState.READY)
            tracker.transition(expected_state)
        elif expected_state is packaged_state.ProcessState.STOPPING:
            tracker.transition(packaged_state.ProcessState.MIGRATING)
            tracker.transition(packaged_state.ProcessState.READY)
            tracker.transition(packaged_state.ProcessState.DRAINING)
            tracker.transition(expected_state)
        elif expected_state is packaged_state.ProcessState.FAILED:
            tracker.fail("test")
        cases.append((expected_state, tracker))

    for expected_state, tracker in cases:
        assert tracker.snapshot().process is expected_state
        assert application_lifecycle.process_state_number(tracker) == list(
            packaged_state.ProcessState
        ).index(expected_state)


def test_web_boundary_no_longer_owns_projection_mapping():
    source = Path("sonder_runtime/adapters/web/lifecycle.py").read_text(
        encoding="utf-8"
    )
    assert "def _state_number" not in source
    assert "_state_number = process_state_number" in source
