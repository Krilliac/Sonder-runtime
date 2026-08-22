from dataclasses import dataclass

import pytest

from sonder_runtime.application.training.qualification import (
    CheapLearningFirst,
    DatasetQualification,
    LockedDependency,
    QualifiedDependencyLock,
    TrainingEvaluation,
    TrainingEvaluationPolicy,
)


def _dependency(name: str, version: str = "1.0", digest: str = "sha:x") -> LockedDependency:
    return LockedDependency(name, version, "internal-index", digest)


def test_exact_lock_reports_missing_extra_duplicate_and_environment_mismatches():
    lock = QualifiedDependencyLock(
        (_dependency("accelerate"), _dependency("torch", "2.4", "sha:torch")),
        "training-lock-v1",
        "environment-sha",
    )
    assert lock.verify_exact(
        environment_digest="environment-sha",
        installed=(_dependency("torch", "2.4", "sha:torch"), _dependency("accelerate")),
    ) == (True, ())
    valid, failures = lock.verify_exact(
        environment_digest="wrong-environment",
        installed=(_dependency("accelerate"), _dependency("accelerate"), _dependency("extra")),
    )
    assert not valid
    assert {"environment_mismatch", "duplicate_installed_dependency", "missing_dependency", "extra_dependency", "dependency_record_mismatch"}.issubset(failures)


def _dataset(**changes):
    values = dict(
        dataset_id="dataset-1",
        snapshot_digest="snapshot-sha",
        source="approved-corpus",
        source_revision="corpus-v4",
        license_id="Apache-2.0",
        privacy_review_id="privacy-review-7",
        privacy_status="approved",
        dedup_digest="dedup-sha",
        dedup_method="content-hash-v2",
        dedup_snapshot_digest="snapshot-sha",
        train_snapshot_digest="train-sha",
        eval_snapshot_digest="eval-sha",
        row_count=100,
    )
    values.update(changes)
    return DatasetQualification(**values)


def test_dataset_gate_requires_privacy_source_license_dedup_and_separation_evidence():
    assert _dataset().validate(
        allowed_licenses=frozenset({"Apache-2.0"}),
        approved_sources=frozenset({"approved-corpus"}),
    ) == ()
    failures = _dataset(
        privacy_status="pending",
        source_revision="",
        license_id="unknown",
        source="unknown",
        dedup_snapshot_digest="other-sha",
        train_eval_overlap_count=1,
        eval_snapshot_digest="train-sha",
    ).validate(
        allowed_licenses=frozenset({"Apache-2.0"}),
        approved_sources=frozenset({"approved-corpus"}),
    )
    assert {"privacy_not_approved", "source_revision_missing", "license_not_allowed", "source_not_approved", "dedup_snapshot_mismatch", "dedup_or_contamination_failure", "train_eval_not_separated"}.issubset(failures)


def test_training_gate_rejects_non_finite_and_all_training_dimensions():
    with pytest.raises(ValueError):
        TrainingEvaluation(float("nan"), 0.0, 1.0, 1.0, 1.0, 1.0)
    policy = TrainingEvaluationPolicy()
    passed, failures = policy.gate(TrainingEvaluation(.79, .051, 1001, 4097, .79, .79))
    assert not passed
    assert set(failures) == {"behavior", "regression", "latency", "memory", "context", "tool_use"}


@dataclass
class _Port:
    reliable: bool
    calls: int = 0

    def can_encode(self, behavior: str) -> bool:
        return self.reliable

    def apply(self, behavior: str) -> str:
        self.calls += 1
        return "receipt-cheap"


@dataclass
class _WeightPort:
    calls: int = 0

    def train(self, behavior: str) -> str:
        self.calls += 1
        return "receipt-weight"


def test_cheap_learning_first_executes_first_reliable_port_and_never_trains_after_success():
    memory = _Port(reliable=True)
    routing = _Port(reliable=True)
    weight = _WeightPort()
    choice = CheapLearningFirst().execute(
        behavior="remember preference",
        methods={"memory": memory, "routing": routing},
        weight_training=weight,
    )
    assert choice.method == "memory"
    assert memory.calls == 1 and routing.calls == 0 and weight.calls == 0


def test_cheap_learning_first_uses_weight_training_only_when_all_cheap_ports_fail():
    weight = _WeightPort()
    choice = CheapLearningFirst().execute(
        behavior="new capability",
        methods={name: _Port(False) for name in ("memory", "retrieval", "skill", "routing", "few_shot")},
        weight_training=weight,
    )
    assert choice.method == "weight_training" and weight.calls == 1
