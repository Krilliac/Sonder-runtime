"""Host-owned deterministic strategy canaries, never live benefit claims."""
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.evaluation.promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES, PromotionGateError, PromotionKind,
    evaluate_promotion_gate,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationMode, ShadowCanaryObservation,
)
from sonder_runtime.application.evaluation.strategy_cases import (
    StrategyEvidenceClass, StrategyEvaluationIdentity, StrategyHeldoutCase, StrategyHeldoutStep,
    compare_strategy_ablations, evaluate_heldout_strategy_case,
)
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.bootstrap.strategy_observers import observe_codegen_build
from sonder_runtime.domain.strategy.models import (
    FailureClass, FailureObservation, ProgressMetric, ProgressVector,
    StrategyAction, StrategyAttempt, StrategyBudget, StrategyError,
    StrategySignature, StrategyUsage,
)


def identity(*, memory="d" * 64, model_roles=None):
    return StrategyEvaluationIdentity(
        "strategy-v1", model_roles or {"coder": "coder-v1", "critic": "critic-v1"},
        "c" * 64, memory, "e" * 64, "f" * 64,
    )


def _attempt(number, *, hypothesis="b" * 64, family="patch", before=2, after=2,
             failure=FailureClass.TEST_FAILURE, outcome="failed", parent="", usage=None,
             intended_change="fix lifetime"):
    vector = lambda value: ProgressVector(
        "a" * 64, (ProgressMetric("failing_tests", value),),
    )
    return StrategyAttempt(
        "heldout-run", f"step-{number}",
        StrategySignature(
            family, "a" * 64, ("src/a.py",), hypothesis, intended_change, "unit-tests",
        ),
        outcome, FailureObservation(failure) if failure is not None else None,
        vector(before), vector(after), usage or StrategyUsage(attempts=1, model_calls=1,
                                                              verifier_calls=1, tokens=20),
        parent_attempt_id=parent, model_route="coder-v1",
    )


def step(number, expected, *, choices=None, budget=None, unresolved=False, **attempt_changes):
    return StrategyHeldoutStep(
        _attempt(number, **attempt_changes), budget or StrategyBudget(attempts=5),
        choices if choices is not None else (
            StrategyAction.REPAIR, StrategyAction.CRITIC, StrategyAction.SWITCH_TOOL,
            StrategyAction.SWITCH_MODEL, StrategyAction.ROLLBACK, StrategyAction.INSPECT,
        ), expected, unresolved_effects=unresolved,
    )


def _run(tmp_path, name, *steps, id=None, scratch=None):
    serial = iter(range(2))
    return evaluate_heldout_strategy_case(
        StrategyHeldoutCase(name, steps), id or identity(),
        trace_factory=lambda: StrategyTraceService(
            SQLiteRuntimeCheckpointRepository(
                tmp_path / f"{scratch or name}-{next(serial)}.db", seal_key=b"x" * 32,
            ),
        ),
        candidate="policy-candidate", baseline="policy-baseline",
    )


@pytest.mark.parametrize("name,steps,expected", [
    ("identical-retry", (
        step(1, StrategyAction.REPAIR), step(2, StrategyAction.CRITIC, parent="step-1"),
    ), True),
    ("disguised-retry", (
        step(1, StrategyAction.REPAIR),
        step(2, StrategyAction.CRITIC, parent="step-1", intended_change="fix   lifetime"),
    ), True),
    ("measured-progress", (
        step(1, StrategyAction.REPAIR),
        step(2, StrategyAction.REPAIR, before=2, after=1, parent="step-1"),
    ), True),
    ("regression-rollback", (
        step(1, StrategyAction.ROLLBACK, before=2, after=3,
             choices=(StrategyAction.ROLLBACK, StrategyAction.INSPECT)),
    ), True),
    ("alternate-tool", (
        step(1, StrategyAction.REPAIR,
             choices=(StrategyAction.REPAIR, StrategyAction.SWITCH_TOOL)),
        step(2, StrategyAction.SWITCH_TOOL, parent="step-1",
             choices=(StrategyAction.REPAIR, StrategyAction.SWITCH_TOOL)),
    ), True),
    ("critic-failure", (
        step(1, StrategyAction.REPAIR), step(2, StrategyAction.CRITIC, parent="step-1"),
        step(3, StrategyAction.INSPECT, parent="step-2", family="critic_review",
             hypothesis="c" * 64, failure=FailureClass.HYPOTHESIS_REJECTED),
    ), True),
    ("critic-completion-boundary", (
        step(1, StrategyAction.REPAIR), step(2, StrategyAction.CRITIC, parent="step-1"),
        step(3, StrategyAction.PAUSE, parent="step-2", family="critic_review",
             hypothesis="c" * 64, failure=None, outcome="succeeded", after=0),
    ), True),
    ("model-rotation", (
        step(1, StrategyAction.SWITCH_MODEL, failure=FailureClass.MODEL_UNAVAILABLE),
    ), True),
    ("recursive-delegation", (
        step(1, StrategyAction.INSPECT, choices=(StrategyAction.INSPECT, StrategyAction.SPAWN_SPECIALIST)),
        step(2, StrategyAction.SPAWN_SPECIALIST, parent="step-1",
             choices=(StrategyAction.SPAWN_SPECIALIST,)),
    ), True),
    ("parallel-hypotheses", (
        step(1, StrategyAction.INSPECT, choices=(StrategyAction.INSPECT, StrategyAction.PARALLEL_HYPOTHESES)),
        step(2, StrategyAction.PARALLEL_HYPOTHESES, parent="step-1",
             choices=(StrategyAction.PARALLEL_HYPOTHESES,)),
    ), True),
    ("exhausted-budget", (
        step(1, StrategyAction.FAIL, budget=StrategyBudget(attempts=1)),
    ), True),
    ("crash-unknown-effect", (
        step(1, StrategyAction.RECONCILE, unresolved=True),
    ), True),
    ("wrong-oracle", (
        step(1, StrategyAction.REPAIR, failure=FailureClass.MODEL_UNAVAILABLE,
             choices=(StrategyAction.SWITCH_MODEL,)),
    ), False),
])
def test_heldout_cases_replay_controller_through_sealed_trace(tmp_path, name, steps, expected):
    result = _run(tmp_path, name, *steps)
    assert result.passed is expected
    assert result.replay_equivalent is True
    assert result.trajectory_digest in result.provenance[1]
    assert any(item.startswith("case_digest:") for item in result.provenance)
    assert result.metrics["attempts"] == len(steps)
    assert result.metrics["tokens"] == len(steps) * 20
    assert result.metrics["pass_rate"] == float(expected)
    if name == "critic-completion-boundary":
        assert result.metrics["task_success"] == 0.0
        assert result.metrics["repair_success"] == 0.0


def test_irrelevant_memory_does_not_change_action_but_changes_suite_identity(tmp_path):
    items = (step(1, StrategyAction.REPAIR), step(2, StrategyAction.CRITIC, parent="step-1"))
    first = _run(tmp_path, "memory-negative", *items, scratch="memory-first")
    second = _run(tmp_path, "memory-negative", *items,
                  id=identity(memory="0" * 64), scratch="memory-second")
    assert first.passed and second.passed
    assert first.trajectory_digest == second.trajectory_digest
    assert first.suite.digest != second.suite.digest
    with pytest.raises(StrategyError, match="visible context"):
        compare_strategy_ablations(first, second)


def test_ablation_differences_are_descriptive_and_keep_run_context_fixed(tmp_path):
    first = _run(tmp_path, "ablation", step(1, StrategyAction.REPAIR))
    variation = _run(tmp_path, "ablation", step(1, StrategyAction.REPAIR,
        usage=StrategyUsage(attempts=1, model_calls=2, tokens=50, critic_calls=1)),
        id=identity(model_roles={"coder": "coder-v1", "critic": "critic-v2"}),
        scratch="ablation-variation")
    differences = compare_strategy_ablations(first, variation)
    assert differences["tokens"] == 30
    assert differences["critic_calls"] == 1
    assert differences["pass_rate"] == 0
    changed_source = _run(tmp_path, "ablation", step(1, StrategyAction.REPAIR, before=3),
                          scratch="ablation-changed-source")
    with pytest.raises(StrategyError, match="same held-out source"):
        compare_strategy_ablations(first, changed_source)


def test_host_codegen_observation_produces_actual_trace_resource_readings(tmp_path):
    trace = StrategyTraceService(SQLiteRuntimeCheckpointRepository(
        tmp_path / "host-hook.db", seal_key=b"x" * 32,
    ))
    for number, errors, critic in ((1, ["main.c: unknown type"], False), (2, [], True)):
        observe_codegen_build(
            trace, run_id="host-codegen", file_name="main.c", project_dir="project",
            spec="compile", build_program="cc main.c", attempt_number=number,
            attempt_limit=4, code="int main(void) { return %d; }" % number,
            before_errors=["main.c: unknown type"], after_errors=errors,
            before_complete=True, after_complete=True, build_ran=True,
            exit_ok=not errors, route="coder", critic_used=critic,
        )
    history = trace.history("host-codegen")
    assert len(trace.observed_decisions("host-codegen")) == 2
    assert [item.usage.model_calls for item in history] == [1, 2]
    assert history[-1].outcome == "succeeded"


def test_strategy_policy_gate_refuses_thin_synthetic_result(tmp_path):
    result = _run(tmp_path, "gate", step(1, StrategyAction.REPAIR))
    shadow = ShadowCanaryObservation(EvaluationMode.SHADOW, "shadow", True, 30, {"errors": 0}, 0)
    canary = ShadowCanaryObservation(EvaluationMode.CANARY, "canary", True, 30, {"errors": 0}, .05)
    decision = evaluate_promotion_gate(
        DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.STRATEGY],
        results=(result,), baseline_pass_rate=1.0, shadow=shadow, canary=canary,
    )
    assert not decision.passed
    assert "gate_failed:sample_size" in decision.reason_codes
    assert "gate_failed:independent_task_receipts" in decision.reason_codes
    assert decision.result_ids == (result.result_id,)


def test_strategy_gate_rejects_generic_or_replayed_graph_evidence(tmp_path):
    result = _run(tmp_path, "bound-gate", step(1, StrategyAction.REPAIR))
    policy = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.STRATEGY]
    with pytest.raises(PromotionGateError, match="attempt graph or runtime provenance"):
        evaluate_promotion_gate(policy, results=(replace(result, trajectory_digest="0" * 64),))
    with pytest.raises(PromotionGateError, match="bound held-out identity"):
        evaluate_promotion_gate(policy, results=(replace(result, suite=replace(
            result.suite, suite_id="generic")),))
    replayed = replace(
        result,
        result_id=f"strategy/replayed-run/{result.trajectory_digest[:16]}",
        provenance=tuple(
            "case:replayed-run" if item.startswith("case:") else item
            for item in result.provenance
        ),
    )
    with pytest.raises(PromotionGateError, match="duplicate strategy attempt graph"):
        evaluate_promotion_gate(policy, results=(result, replayed))
    same_case_new_attempt = _run(
        tmp_path, "bound-gate", step(
            1, StrategyAction.REPAIR,
            usage=StrategyUsage(attempts=1, model_calls=1, verifier_calls=1, tokens=21),
        ), scratch="bound-gate-new-attempt",
    )
    assert same_case_new_attempt.trajectory_digest != result.trajectory_digest
    with pytest.raises(PromotionGateError, match="duplicate strategy held-out case"):
        evaluate_promotion_gate(policy, results=(result, same_case_new_attempt))


def test_invalid_context_or_invalid_attempt_graph_fails_closed(tmp_path):
    with pytest.raises(StrategyError, match="SHA-256"):
        identity(memory="invalid")
    bad = step(1, StrategyAction.REPAIR, parent="missing-parent")
    with pytest.raises(StrategyError, match="absent parent"):
        _run(tmp_path, "missing-parent", bad)
    shared = StrategyTraceService(SQLiteRuntimeCheckpointRepository(
        tmp_path / "shared.db", seal_key=b"x" * 32,
    ))
    with pytest.raises(StrategyError, match="independent trace"):
        evaluate_heldout_strategy_case(
            StrategyHeldoutCase("shared-replay", (step(1, StrategyAction.REPAIR),)),
            identity(), trace_factory=lambda: shared, candidate="x", baseline="y",
        )


def test_thirty_genuine_sealed_synthetic_canaries_cannot_approve_task_strategy(tmp_path):
    from sonder_runtime.adapters.evaluation_corpus import EvaluationCorpusSource
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.application.evaluation.corpus_inventory import CorpusSourceKind, CorpusSourceSpec
    from sonder_runtime.application.evaluation.proposal_lifecycle import EvaluationLifecycleError
    from sonder_runtime.bootstrap.evaluation import compose_evaluation_service

    sources = tuple(EvaluationCorpusSource(
        CorpusSourceSpec(kind.value, kind),
        lambda **_bounds: ({"id": "bounded-host-row"},),
    ) for kind in CorpusSourceKind)
    service = compose_evaluation_service(SQLiteSessionRepository(tmp_path / "sessions.db"), sources=sources)
    suite = identity().suite()
    service.create_proposal("synthetic-only", "policy-candidate", "policy-baseline", suite,
                            kind=PromotionKind.STRATEGY)
    service.submit("synthetic-only")
    service.begin_evaluation("synthetic-only")
    for index in range(30):
        proposed = step(1, StrategyAction.REPAIR)
        proposed = replace(proposed, attempt=replace(proposed.attempt, run_id=f"canary-{index}"))
        result = _run(tmp_path, f"canary-{index}", proposed)
        assert result.passed and result.metrics["task_success"] == 0.0
        assert f"strategy_evidence_class:{StrategyEvidenceClass.SYNTHETIC_POLICY_CANARY.value}" in result.provenance
        service.record_result("synthetic-only", result)
    service.begin_shadow("synthetic-only")
    service.record_observation("synthetic-only", ShadowCanaryObservation(
        EvaluationMode.SHADOW, "healthy-shadow", True, 30, {"errors": 0}, 0,
    ))
    service.begin_canary("synthetic-only")
    service.record_observation("synthetic-only", ShadowCanaryObservation(
        EvaluationMode.CANARY, "healthy-canary", True, 30, {"errors": 0}, .05,
    ))
    decision = service.promotion_gate_decision(
        "synthetic-only", baseline_pass_rate=1.0, case_regressions=0,
    )
    assert decision.samples == 30 and decision.pass_rate == 1.0
    assert not decision.passed
    assert "gate_failed:independent_task_receipts" in decision.reason_codes
    evidence = service.gated_promotion_evidence(
        "synthetic-only", baseline_pass_rate=1.0, case_regressions=0,
        holdout_passed=True, rollback_reference="baseline", provenance=("synthetic-policy-case",),
    )
    with pytest.raises(EvaluationLifecycleError, match="absent, stale, or rejected"):
        service.approve("synthetic-only", evidence.digest)
    with pytest.raises(PromotionGateError, match="typed evidence class"):
        evaluate_promotion_gate(
            DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.STRATEGY],
            results=(replace(result, provenance=tuple(
                item for item in result.provenance if not item.startswith("strategy_evidence_class:")
            )),),
        )
    claimed_success = replace(result, metrics={**result.metrics, "task_success": 1.0})
    with pytest.raises(EvaluationLifecycleError, match="cannot claim measured task success"):
        service.record_result("synthetic-only", claimed_success)
    with pytest.raises(PromotionGateError, match="cannot claim measured task success"):
        evaluate_promotion_gate(
            DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.STRATEGY],
            results=(claimed_success,),
        )
