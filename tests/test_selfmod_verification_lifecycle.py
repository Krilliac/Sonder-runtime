from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect

import pytest

from sonder_runtime.application.selfmod.verification_lifecycle import (
    ActivationRecord,
    BackupRecord,
    FailureState,
    HealthRecord,
    LifecycleConflict,
    LifecycleInputError,
    LifecyclePhase,
    ReviewRecord,
    RollbackRecord,
    VerificationKind,
    VerificationLifecycle,
    VerificationRecord,
)


DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def verification(kind: VerificationKind, suffix: str, passed: bool = True) -> VerificationRecord:
    return VerificationRecord(f"v-{suffix}", kind, passed, DIGEST, f"{kind.value} result")


def lifecycle() -> VerificationLifecycle:
    flow = VerificationLifecycle()
    flow.propose("candidate-1", "bounded change", DIGEST)
    for kind, suffix in (
        (VerificationKind.TARGETED, "targeted"),
        (VerificationKind.ARCHITECTURE, "architecture"),
        (VerificationKind.REGRESSION, "regression"),
        (VerificationKind.SMOKE, "smoke"),
    ):
        flow.record_verification("candidate-1", verification(kind, suffix))
    flow.record_review("candidate-1", ReviewRecord("review-1", "reviewer-1", True, tuple(f"v-{x}" for x in ("targeted", "architecture", "regression", "smoke"))))
    return flow


def test_all_required_verification_kinds_are_required_before_review() -> None:
    flow = VerificationLifecycle()
    flow.propose("candidate-1", "objective", DIGEST)
    flow.record_verification("candidate-1", verification(VerificationKind.TARGETED, "targeted"))
    assert flow.get("candidate-1").phase is LifecyclePhase.VERIFYING
    with pytest.raises(LifecycleConflict):
        flow.record_review("candidate-1", ReviewRecord("review", "reviewer", True, ("v-targeted",)))


def test_successful_lifecycle_records_all_evidence_without_side_effects() -> None:
    flow = lifecycle()
    backup = flow.record_backup("candidate-1", BackupRecord("backup", True, OTHER_DIGEST, "backup receipt"))
    assert backup.phase is LifecyclePhase.BACKED_UP
    activation = flow.record_activation("candidate-1", ActivationRecord("activation", True, DIGEST, "activation receipt"))
    assert activation.phase is LifecyclePhase.ACTIVATED
    complete = flow.record_health("candidate-1", HealthRecord("health", True, DIGEST, "healthy"))
    assert complete.phase is LifecyclePhase.COMPLETED
    assert complete.failure is FailureState.NONE
    assert complete.backup is not None and complete.activation is not None and complete.health is not None
    assert complete.rollback is None


def test_health_failure_requires_explicit_successful_rollback() -> None:
    flow = lifecycle()
    flow.record_backup("candidate-1", BackupRecord("backup", True, OTHER_DIGEST))
    flow.record_activation("candidate-1", ActivationRecord("activation", True, DIGEST))
    failed = flow.record_health("candidate-1", HealthRecord("health", False, DIGEST, "health failed"))
    assert failed.phase is LifecyclePhase.HEALTH_FAILED
    assert failed.failure is FailureState.HEALTH_FAILED
    rolled_back = flow.record_rollback("candidate-1", RollbackRecord("rollback", True, OTHER_DIGEST, "restored baseline"))
    assert rolled_back.phase is LifecyclePhase.ROLLED_BACK
    assert rolled_back.failure is FailureState.NONE


@pytest.mark.parametrize(
    ("method", "record_type", "failure"),
    [
        ("record_backup", BackupRecord, FailureState.BACKUP_FAILED),
        ("record_activation", ActivationRecord, FailureState.ACTIVATION_FAILED),
    ],
)
def test_backup_and_activation_failures_are_explicit(method: str, record_type: type, failure: FailureState) -> None:
    flow = lifecycle()
    if method == "record_activation":
        flow.record_backup("candidate-1", BackupRecord("backup", True, OTHER_DIGEST))
    result = getattr(flow, method)("candidate-1", record_type("evidence", False, DIGEST, "failed"))
    assert result.phase is LifecyclePhase.FAILED
    assert result.failure is failure


def test_verification_failure_is_terminal_and_cannot_be_reviewed() -> None:
    flow = VerificationLifecycle()
    flow.propose("candidate-1", "objective", DIGEST)
    result = flow.record_verification("candidate-1", verification(VerificationKind.TARGETED, "targeted", False))
    assert result.phase is LifecyclePhase.FAILED
    assert result.failure is FailureState.VERIFICATION_FAILED
    with pytest.raises(LifecycleConflict):
        flow.record_review("candidate-1", ReviewRecord("review", "reviewer", True, ("v-targeted",)))


def test_review_must_be_independent_and_cite_every_verification() -> None:
    flow = VerificationLifecycle()
    flow.propose("candidate-1", "bounded change", DIGEST)
    for kind, suffix in (
        (VerificationKind.TARGETED, "targeted"),
        (VerificationKind.ARCHITECTURE, "architecture"),
        (VerificationKind.REGRESSION, "regression"),
        (VerificationKind.SMOKE, "smoke"),
    ):
        flow.record_verification("candidate-1", verification(kind, suffix))
    with pytest.raises(LifecycleInputError):
        flow.record_review("candidate-1", ReviewRecord("review", "reviewer", True, ("v-targeted",)))
    rejected = flow.record_review("candidate-1", ReviewRecord("review", "reviewer", False, tuple(f"v-{x}" for x in ("targeted", "architecture", "regression", "smoke"))))
    assert rejected.phase is LifecyclePhase.FAILED
    assert rejected.failure is FailureState.REVIEW_FAILED


def test_review_independence_is_explicit_evidence() -> None:
    flow = VerificationLifecycle()
    flow.propose("candidate-1", "bounded change", DIGEST)
    for kind, suffix in (
        (VerificationKind.TARGETED, "targeted"),
        (VerificationKind.ARCHITECTURE, "architecture"),
        (VerificationKind.REGRESSION, "regression"),
        (VerificationKind.SMOKE, "smoke"),
    ):
        flow.record_verification("candidate-1", verification(kind, suffix))
    result = flow.record_review(
        "candidate-1",
        ReviewRecord(
            "review", "reviewer", True,
            tuple(f"v-{x}" for x in ("targeted", "architecture", "regression", "smoke")),
            independent=False,
        ),
    )
    assert result.phase is LifecyclePhase.FAILED
    assert result.failure is FailureState.REVIEW_REQUIRED


def test_rollback_failure_is_explicit() -> None:
    flow = lifecycle()
    flow.record_backup("candidate-1", BackupRecord("backup", True, OTHER_DIGEST))
    flow.record_activation("candidate-1", ActivationRecord("activation", True, DIGEST))
    flow.record_health("candidate-1", HealthRecord("health", False, DIGEST))
    result = flow.record_rollback("candidate-1", RollbackRecord("rollback", False, OTHER_DIGEST, "restore failed"))
    assert result.phase is LifecyclePhase.FAILED
    assert result.failure is FailureState.ROLLBACK_FAILED


def test_records_are_typed_frozen_and_snapshot_is_deterministic() -> None:
    flow = VerificationLifecycle()
    flow.propose("z", "objective", DIGEST)
    flow.propose("a", "objective", DIGEST)
    assert tuple(item.candidate_id for item in flow.snapshot()) == ("a", "z")
    record = flow.get("a")
    with pytest.raises(FrozenInstanceError):
        record.phase = LifecyclePhase.VERIFIED  # type: ignore[misc]
    assert "subprocess" not in inspect.getsource(VerificationLifecycle)
    assert "open(" not in inspect.getsource(VerificationLifecycle)


@pytest.mark.parametrize(
    "factory,args",
    [
        (VerificationRecord, ("id", VerificationKind.TARGETED, True, "bad")),
        (BackupRecord, ("id", True, "bad")),
        (HealthRecord, ("id", True, "bad")),
    ],
)
def test_artifact_digests_are_required_for_auditable_evidence(factory: type, args: tuple[object, ...]) -> None:
    with pytest.raises(LifecycleInputError):
        factory(*args)


def test_proposals_and_records_are_not_exposed_as_mutable_aliases() -> None:
    flow = VerificationLifecycle()
    original = flow.propose("candidate-1", "objective", DIGEST)
    updated = flow.record_verification("candidate-1", verification(VerificationKind.TARGETED, "targeted"))
    assert original.verifications == ()
    assert updated.verifications == (verification(VerificationKind.TARGETED, "targeted"),)
    assert flow.get("candidate-1") == updated
