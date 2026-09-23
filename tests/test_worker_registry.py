from sonder_runtime.adapters.persistence.sqlite.worker_registry import SQLiteWorkerRegistry
from sonder_runtime.application.ports.worker_registry import (
    DuplicateWorkerError, WorkerLaunch, WorkerStatus, WorkerRegistryError,
)


def launch(worker="w1", key="resume-1"):
    return WorkerLaunch(
        worker, "parent", "editor", "model-a", "local", "high",
        ("repo",), ("read", "test"), {"steps": 3}, {"max_attempts": 2}, key, key,
    )


def test_registry_round_trips_execution_contract_and_reopens(tmp_path):
    path = tmp_path / "workers.sqlite"
    first = SQLiteWorkerRegistry(path)
    admitted = first.admit(launch())
    assert admitted.status is WorkerStatus.QUEUED
    running = first.start("w1", expected_revision=admitted.revision)
    assert running and running.status is WorkerStatus.RUNNING
    second = SQLiteWorkerRegistry(path)
    restored = second.get("w1")
    assert restored == running
    assert restored.launch.model == "model-a"
    assert restored.launch.allowed_tools == ("read", "test")


def test_active_duplicate_resume_key_is_rejected_atomically(tmp_path):
    registry = SQLiteWorkerRegistry(tmp_path / "duplicates.sqlite")
    registry.admit(launch())
    try:
        registry.admit(launch(worker="w2"))
    except DuplicateWorkerError:
        pass
    else:
        raise AssertionError("active resume key must be single-owner")


def test_terminal_worker_can_be_reopened_for_resume_and_records_verification(tmp_path):
    registry = SQLiteWorkerRegistry(tmp_path / "resume.sqlite")
    queued = registry.admit(launch())
    running = registry.start("w1", expected_revision=queued.revision)
    failed = registry.finish("w1", status=WorkerStatus.FAILED, verification={"tests": "failed"}, error="boom", expected_revision=running.revision)
    assert failed and failed.status is WorkerStatus.FAILED
    resumed = registry.start("w1", expected_revision=failed.revision)
    assert resumed and resumed.status is WorkerStatus.RUNNING
    assert resumed.launch.resume_key == "resume-1"


def test_transitions_are_fail_closed_and_retry_attempts_are_bounded(tmp_path):
    registry = SQLiteWorkerRegistry(tmp_path / "states.sqlite")
    queued = registry.admit(launch())
    assert registry.progress("w1", {"step": 1}, expected_revision=queued.revision) is None
    assert registry.finish("w1", status=WorkerStatus.FAILED, verification={}, expected_revision=queued.revision) is None
    running = registry.start("w1", expected_revision=queued.revision)
    assert running and running.attempt_count == 1
    assert registry.start("w1", expected_revision=running.revision) is None
    failed = registry.finish("w1", status=WorkerStatus.FAILED, verification={"tests": "failed"}, expected_revision=running.revision)
    assert failed
    resumed = registry.start("w1", expected_revision=failed.revision)
    assert resumed and resumed.attempt_count == 2
    failed_again = registry.finish("w1", status=WorkerStatus.FAILED, verification={"tests": "failed"}, expected_revision=resumed.revision)
    assert failed_again
    try:
        registry.start("w1", expected_revision=failed_again.revision)
    except WorkerRegistryError as error:
        assert "exhausted" in str(error)
    else:
        raise AssertionError("max_attempts must be enforced")


def test_compare_and_set_rejects_stale_progress(tmp_path):
    registry = SQLiteWorkerRegistry(tmp_path / "cas.sqlite")
    queued = registry.admit(launch())
    running = registry.start("w1", expected_revision=queued.revision)
    assert running
    assert registry.progress("w1", {"step": 1}, expected_revision=queued.revision) is None
    assert registry.get("w1").revision == running.revision
