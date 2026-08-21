from sonder_runtime.platform.service_state import ProcessState, ServiceStateTracker


def test_recovery_required_is_distinct_and_non_serving():
    tracker = ServiceStateTracker()
    tracker.transition(ProcessState.MIGRATING, "migration")
    snapshot = tracker.transition(ProcessState.RECOVERY_REQUIRED, "recovery needed")

    assert snapshot.process is ProcessState.RECOVERY_REQUIRED
    assert not snapshot.serving
    assert tracker.ready_for_traffic() == (
        False, "process state is recovery_required"
    )


def test_recovery_required_can_be_explicitly_recovered():
    tracker = ServiceStateTracker()
    tracker.transition(ProcessState.MIGRATING, "migration")
    tracker.transition(ProcessState.RECOVERY_REQUIRED, "recovery needed")
    tracker.transition(ProcessState.READY, "recovery complete")

    assert tracker.snapshot().process is ProcessState.READY
