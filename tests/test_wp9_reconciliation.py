from sonder_runtime.application.operations.startup_reconciliation import (
    DrainAction, RecordKind, ReconciliationClass, StartupObservation,
    build_drain_plan, reconcile_observation,
)


def test_interrupted_checkpointed_job_is_resumable_and_terminal_state_is_not():
    result = reconcile_observation(StartupObservation(
        RecordKind.JOB, "job-1", "interrupted", checkpoint_available=True,
    ))
    assert result.classification is ReconciliationClass.RESUMABLE
    assert result.action is DrainAction.RESUME
    terminal = reconcile_observation(StartupObservation(RecordKind.JOB, "job-2", "succeeded"))
    assert terminal.classification is ReconciliationClass.TERMINAL
    assert terminal.action is DrainAction.SKIP


def test_dead_owner_is_orphaned_and_process_cleanup_is_only_an_intent():
    result = reconcile_observation(StartupObservation(
        RecordKind.SUBAGENT, "child-1", "running", owner_instance_id="old",
        owner_alive=False, process_id=42, process_group_id=42,
    ))
    assert result.classification is ReconciliationClass.ORPHANED
    assert result.action is DrainAction.CLEANUP_PROCESS_TREE


def test_drain_plan_is_bounded_disables_new_work_and_handles_outbox_delivery():
    observations = [
        StartupObservation(RecordKind.OUTBOX, "evt-1", "unpublished", published=False),
        StartupObservation(RecordKind.JOB, "job-1", "running", owner_instance_id="me", owner_alive=True),
        StartupObservation(RecordKind.JOB, "job-2", "queued"),
    ]
    plan = build_drain_plan(observations, max_records=2)
    assert not plan.accept_new_work and not plan.allow_new_claims
    assert [item.action for item in plan.results] == [DrainAction.DELIVER, DrainAction.KEEP]
    assert plan.truncated is True


def test_invalid_process_and_bounds_are_rejected():
    try:
        StartupObservation(RecordKind.JOB, "job", "running", process_id=0)
        assert False
    except ValueError:
        pass
    try:
        build_drain_plan([], max_records=0)
        assert False
    except ValueError:
        pass
