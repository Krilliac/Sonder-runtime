"""Focused contracts for durable agent/job state-machine hardening."""
from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.adapters.persistence.sqlite.workflow_checkpoints import (
    SQLiteWorkflowCheckpointRepository,
)
from sonder_runtime.application.capabilities.jobs import (
    JobRegistryService,
    ResumableWorkflowEngine,
)
from sonder_runtime.application.ports.jobs import (
    JobIdentity,
    JobStatus,
    WorkflowDefinition,
    WorkflowStep,
)
from sonder_runtime.domain.common.errors import ConcurrencyConflict, InvalidInput
from sonder_runtime.domain.task_ledger import build_task_ledger


def _identity(job_id: str, *, parent: str | None = None) -> JobIdentity:
    return JobIdentity(
        job_id, "workflow", f"operation-{job_id}", f"idempotency-{job_id}",
        parent_job_id=parent,
    )


def test_lease_claim_tokens_fence_reused_worker_identity(tmp_path) -> None:
    now = ["2026-08-22T12:00:00Z"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    registry.create(_identity("job"))

    first = registry.claim("job", "worker", lease_seconds=1)
    assert first is not None and first.claim_token and first.attempt == 1
    now[0] = "2026-08-22T12:00:02Z"
    second = registry.claim("job", "worker", lease_seconds=30)
    assert second is not None and second.claim_token != first.claim_token
    assert second.attempt == 2

    assert registry.heartbeat(
        "job", "worker", claim_token=first.claim_token,
    ) is False
    assert registry.finish_once(
        "job", "worker", JobStatus.SUCCEEDED, receipt_key="receipt",
        result={"ok": True}, claim_token=first.claim_token,
    ) is None
    assert registry.finish_once(
        "job", "worker", JobStatus.SUCCEEDED, receipt_key="receipt",
        result={"ok": True}, claim_token=second.claim_token,
    ) is not None


def test_completion_receipt_replays_exactly_once_and_survives_reopen(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    registry = SQLiteDurableJobRegistry(path)
    registry.create(_identity("job"))
    claim = registry.claim("job", "worker")
    assert claim is not None

    first = registry.finish_once(
        "job", "worker", JobStatus.SUCCEEDED,
        receipt_key="commit:job:1", result={"artifact": "sha256:abc"},
        claim_token=claim.claim_token,
    )
    replay = registry.finish_once(
        "job", "worker", JobStatus.SUCCEEDED,
        receipt_key="commit:job:1", result={"artifact": "sha256:abc"},
        claim_token=claim.claim_token,
    )
    assert replay == first
    with pytest.raises(ValueError, match="conflicts"):
        registry.finish_once(
            "job", "worker", JobStatus.SUCCEEDED,
            receipt_key="different", result={"artifact": "sha256:abc"},
            claim_token=claim.claim_token,
        )
    assert SQLiteDurableJobRegistry(path).completion_receipt("job") == first


def test_receipt_key_conflict_rolls_back_other_job_terminal_update(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    claims = {}
    for job_id in ("a", "b"):
        registry.create(_identity(job_id))
        claims[job_id] = registry.claim(job_id, "worker")
    registry.finish_once(
        "a", "worker", JobStatus.SUCCEEDED, receipt_key="global-key",
        result={"job": "a"}, claim_token=claims["a"].claim_token,
    )
    with pytest.raises(ValueError, match="already committed"):
        registry.finish_once(
            "b", "worker", JobStatus.SUCCEEDED, receipt_key="global-key",
            result={"job": "b"}, claim_token=claims["b"].claim_token,
        )
    assert registry.get("b").status is JobStatus.CLAIMED
    assert registry.completion_receipt("b") is None


def test_retry_budget_is_persisted_and_revision_guarded(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.create(_identity("job"), metadata={"max_attempts": 2})
    first = registry.claim("job", "worker-a")
    assert first is not None and first.attempt == 1
    failed = registry.finish(
        "job", "worker-a", JobStatus.FAILED, error="transient",
        claim_token=first.claim_token,
    )
    assert failed is not None
    assert registry.retry("job", expected_revision=failed.revision + 1) is None
    queued = registry.retry("job", expected_revision=failed.revision)
    assert queued is not None and queued.status is JobStatus.PENDING

    second = registry.claim("job", "worker-b")
    assert second is not None and second.attempt == 2
    failed_again = registry.finish(
        "job", "worker-b", JobStatus.FAILED, error="permanent",
        claim_token=second.claim_token,
    )
    assert failed_again is not None
    with pytest.raises(ValueError, match="exhausted"):
        registry.retry("job", expected_revision=failed_again.revision)


def test_retry_budget_configuration_has_a_hard_upper_bound(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    with pytest.raises(ValueError, match="between 1 and 100"):
        registry.create(_identity("job"), metadata={"max_attempts": 101})
    assert registry.get("job") is None


def test_prior_attempt_receipt_replays_after_retry(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.create(_identity("job"), metadata={"max_attempts": 2})
    first_claim = registry.claim("job", "worker-a")
    first_receipt = registry.finish_once(
        "job", "worker-a", JobStatus.FAILED, receipt_key="attempt-one",
        error="retryable", claim_token=first_claim.claim_token,
    )
    failed = registry.get("job")
    registry.retry("job", expected_revision=failed.revision)
    second_claim = registry.claim("job", "worker-b")
    assert second_claim.attempt == 2

    replay = registry.finish_once(
        "job", "worker-a", JobStatus.FAILED, receipt_key="attempt-one",
        error="retryable", claim_token=first_claim.claim_token,
    )
    assert replay == first_receipt
    assert registry.get("job").status is JobStatus.CLAIMED


def test_stale_reconciliation_is_bounded_repeatable_and_diagnostic(tmp_path) -> None:
    now = ["2026-08-22T12:00:00Z"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    for job_id in ("a", "b", "c"):
        registry.create(_identity(job_id))
        assert registry.claim(job_id, "worker", lease_seconds=1) is not None
    now[0] = "2026-08-22T12:00:02Z"

    first = registry.reconcile_stale(now=now[0], max_records=2)
    assert first.scanned == first.interrupted == 2
    assert first.interrupted_job_ids == ("a", "b")
    assert first.truncated is True
    second = registry.reconcile_stale(now=now[0], max_records=2)
    assert second.interrupted_job_ids == ("c",)
    assert second.truncated is False


def test_descendant_cancellation_bound_rolls_back_before_any_mutation(tmp_path) -> None:
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.create(_identity("parent"))
    registry.create(_identity("child-a", parent="parent"))
    registry.create(_identity("child-b", parent="parent"))
    claim = registry.claim("child-a", "worker")
    assert claim is not None

    with pytest.raises(ValueError, match="max_descendants"):
        registry.cancel("parent", max_descendants=1)
    assert registry.get("parent").status is JobStatus.PENDING
    assert registry.get("child-a").status is JobStatus.CLAIMED

    cancelled = registry.cancel("parent", max_descendants=2)
    assert [record.identity.job_id for record in cancelled] == [
        "parent", "child-a", "child-b",
    ]
    assert all(record.status is JobStatus.CANCELLED for record in cancelled)
    assert registry.finish(
        "child-a", "worker", JobStatus.SUCCEEDED,
        claim_token=claim.claim_token,
    ).status is JobStatus.CANCELLED


def test_dependency_schedule_rejects_cycles_and_bounds_ready_fanout() -> None:
    tasks = [
        {"id": "a", "title": "A", "status": "done"},
        {"id": "b", "title": "B", "status": "pending"},
        {"id": "c", "title": "C", "status": "pending"},
        {"id": "d", "title": "D", "status": "pending"},
    ]
    ledger = build_task_ledger(
        "goal", tasks, {"b": ("a",), "c": ("a",), "d": ("b", "c")},
    )
    assert [item.task_id for item in ledger.ready_items(max_fanout=1)] == ["b"]
    assert ledger.blocked_dependencies() == {"d": ("b", "c")}
    assert ledger.dependency_batches(max_fanout=1) == (
        ("a",), ("b",), ("c",), ("d",),
    )
    with pytest.raises(InvalidInput, match="cycle"):
        build_task_ledger(
            "goal", tasks[:2], {"a": ("b",), "b": ("a",)},
        )


def test_workflow_checkpoint_accepts_only_current_fenced_claim(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    jobs = JobRegistryService(SQLiteDurableJobRegistry(path))
    engine = ResumableWorkflowEngine(
        jobs, SQLiteWorkflowCheckpointRepository(path),
    )
    engine.start(
        _identity("workflow"),
        WorkflowDefinition("workflow", (WorkflowStep("step", "Step"),)),
    )
    resumed = engine.resume("workflow", "worker")
    assert resumed.claim is not None and resumed.claim.claim_token
    with pytest.raises(ConcurrencyConflict, match="heartbeat rejected"):
        engine.checkpoint(
            "workflow", "worker", next_step=1, state={},
            completed_step_id="step", claim_token="stale-token",
        )
    saved = engine.checkpoint(
        "workflow", "worker", next_step=1, state={},
        completed_step_id="step", claim_token=resumed.claim.claim_token,
    )
    assert saved.next_step == 1


def test_legacy_job_schema_is_adopted_without_rewriting_records(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE durable_job ("
            "job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, operation_id TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL, parent_job_id TEXT, parent_session_id TEXT, "
            "status TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, result_json TEXT, error TEXT NOT NULL, "
            "process_id INTEGER, process_group_id INTEGER, "
            "output_next INTEGER NOT NULL DEFAULT 1, "
            "output_dropped_before INTEGER NOT NULL DEFAULT 0, "
            "worker_id TEXT, lease_until TEXT)"
        )
        connection.execute(
            "INSERT INTO durable_job(job_id,kind,operation_id,idempotency_key,status,"
            "revision,created_at,updated_at,error) VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacy", "workflow", "op", "idem", "pending", 0, "t", "t", ""),
        )

    registry = SQLiteDurableJobRegistry(path)
    record = registry.get("legacy")
    assert record is not None
    assert (record.attempt, record.max_attempts) == (0, 3)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(durable_job)")}
        assert {"attempt", "max_attempts", "claim_token"} <= columns
        assert connection.execute(
            "SELECT COUNT(*) FROM durable_job_receipt"
        ).fetchone()[0] == 0
