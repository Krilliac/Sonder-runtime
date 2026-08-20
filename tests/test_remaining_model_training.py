from sonder_runtime.application.model_gateway.escalation import (
    ControlledEscalation, EscalationRequest, EscalationRoute,
)
from sonder_runtime.application.training.qualification import (
    CheapLearningFirst, DatasetQualification, LockedDependency,
    QualifiedDependencyLock, TrainingEvaluation, TrainingEvaluationPolicy,
)


def routes():
    return (EscalationRoute("small", "q4-small", 0), EscalationRoute("strong", "q4-strong", 1), EscalationRoute("oracle", "q8-oracle", 2))


def test_verifier_failure_escalates_only_within_declared_routes_and_is_bounded():
    policy = ControlledEscalation()
    request = EscalationRequest("r1", routes()[:2], "small", verifier_passed=False, max_escalations=1)
    decision = policy.decide(request)
    assert decision.selected_route.route_id == "strong"
    assert decision.trigger == "verifier_failure"
    assert decision.escalated and not decision.can_escalate
    outcome = policy.record_outcome(request, decision, helped=True, evidence="verifier-pass:r2")
    assert outcome.helped is True
    denied = policy.decide(EscalationRequest("r2", routes()[:2], "strong", verifier_passed=False, escalation_count=1, max_escalations=1))
    assert denied.selected_route.route_id == "strong" and not denied.escalated


def test_escalation_cannot_select_undeclared_route():
    request = EscalationRequest("r1", routes()[:2], "small", uncertainty=0.9)
    decision = ControlledEscalation().decide(request)
    assert decision.selected_route.route_id != "oracle"


def test_exact_dependency_lock_requires_environment_and_all_artifacts():
    deps = (LockedDependency("torch", "2.4", "pypi", "sha:torch"), LockedDependency("transformers", "4.45", "pypi", "sha:tf"))
    lock = QualifiedDependencyLock(deps, "train-lock-1", "env:abc")
    assert lock.verify(environment_digest="env:abc", installed=tuple(reversed(deps)))
    assert not lock.verify(environment_digest="env:other", installed=deps)
    assert not lock.verify(environment_digest="env:abc", installed=(deps[0],))


def dataset(**overrides):
    values = dict(dataset_id="ds", snapshot_digest="sha:data", source="curated", license_id="MIT", privacy_review_id="privacy-1", dedup_digest="sha:dedup", train_snapshot_digest="sha:train", eval_snapshot_digest="sha:eval", row_count=10)
    values.update(overrides)
    return DatasetQualification(**values)


def test_dataset_qualification_checks_provenance_privacy_license_dedup_and_separation():
    assert dataset().validate(allowed_licenses=frozenset({"MIT"}), approved_sources=frozenset({"curated"})) == ()
    failures = dataset(license_id="unknown", source="untrusted", duplicate_count=2).validate(allowed_licenses=frozenset({"MIT"}), approved_sources=frozenset({"curated"}))
    assert {"license_not_allowed", "source_not_approved", "dedup_or_contamination_failure"}.issubset(failures)
    assert "train_eval_not_separated" in dataset(eval_snapshot_digest="sha:train").validate(allowed_licenses=frozenset({"MIT"}), approved_sources=frozenset({"curated"}))


def test_training_specific_evaluation_gate_covers_behavior_regression_latency_memory_context_and_tools():
    policy = TrainingEvaluationPolicy()
    assert policy.gate(TrainingEvaluation(.9, .01, 500, 1000, .9, .9))[0]
    passed, failures = policy.gate(TrainingEvaluation(.7, .2, 2000, 5000, .7, .6))
    assert not passed
    assert set(failures) == {"behavior", "regression", "latency", "memory", "context", "tool_use"}


def test_cheap_learning_first_prefers_reliable_non_weight_change():
    policy = CheapLearningFirst()
    assert policy.choose(reliable_methods={"weight_training", "routing", "memory"}, behavior="formatting").method == "memory"
    assert policy.choose(reliable_methods=set(), behavior="novel reasoning").method == "weight_training"
