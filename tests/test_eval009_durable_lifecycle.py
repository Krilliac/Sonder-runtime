from __future__ import annotations

import pytest

from sonder_runtime.adapters.evaluation_lifecycle import SessionEvaluationLifecycleRepository
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.evaluation.durable_lifecycle import EvaluationLifecycleService
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension, EvaluationLifecycleError, EvaluationMode, EvaluationResult,
    EvaluationSuite, ProposalLifecycle, ProposalState, ShadowCanaryObservation,
)


def _suite() -> EvaluationSuite:
    return EvaluationSuite("quality", "v1", (EvaluationDimension("split", "holdout"),), ("accuracy",))


def _result(suite: EvaluationSuite, result_id: str, mode: EvaluationMode) -> EvaluationResult:
    return EvaluationResult(result_id, suite, "candidate", "baseline", mode, suite.dimensions,
                            {"accuracy": .99}, True, 10, replay_equivalent=True,
                            provenance=("eval:local",))


def _service(path) -> EvaluationLifecycleService:
    repository = SessionEvaluationLifecycleRepository(SQLiteSessionRepository(path))
    return EvaluationLifecycleService(ProposalLifecycle(), repository)


def _ready(service: EvaluationLifecycleService) -> str:
    suite = _suite()
    service.create("p1", "candidate", "baseline", suite)
    service.submit("p1")
    service.begin_evaluation("p1")
    service.record_result("p1", _result(suite, "offline", EvaluationMode.OFFLINE))
    service.begin_shadow("p1")
    service.record_observation("p1", ShadowCanaryObservation(EvaluationMode.SHADOW, "shadow-1", True, 10, {"error": 0}, 0))
    service.begin_canary("p1")
    service.record_result("p1", _result(suite, "canary", EvaluationMode.CANARY))
    service.record_observation("p1", ShadowCanaryObservation(EvaluationMode.CANARY, "canary-1", True, 10, {"error": 0}, .1))
    evidence = service.build_promotion_evidence("p1", gate_results={"quality": True},
        replay_equivalent=True, holdout_passed=True, rollback_reference="baseline",
        provenance=("eval:local",))
    service.approve("p1", evidence.digest)
    return evidence.digest


def test_successful_attended_promotion_and_rollback_are_durable(tmp_path) -> None:
    path = tmp_path / "evaluation.sqlite"
    service = _service(path)
    digest = _ready(service)
    with pytest.raises(EvaluationLifecycleError, match="attended"):
        service.promote("p1", digest)
    assert service.promote("p1", digest, attended=True).state is ProposalState.PROMOTED
    assert service.rollback("p1", attended=True).state is ProposalState.ROLLED_BACK

    reopened = _service(path)
    events = reopened.history("p1")
    assert events[-2].event_type == "evaluation.proposal.promoted"
    assert events[-1].event_type == "evaluation.proposal.rolled_back"
    assert any(event.payload.get("evidence", {}).get("evidence_digest") == digest for event in events)
    assert SQLiteSessionRepository(path).inspect_integrity("evaluation:p1").valid


def test_rejected_transition_is_not_persisted_and_evidence_is_immutable(tmp_path) -> None:
    path = tmp_path / "evaluation.sqlite"
    service = _service(path)
    digest = _ready(service)
    before = len(service.history("p1"))
    with pytest.raises(EvaluationLifecycleError, match="attended"):
        service.rollback("p1")
    assert len(service.history("p1")) == before
    evidence_events = [event for event in service.history("p1")
                       if event.event_type == "evaluation.evidence.attached"]
    assert len(evidence_events) == 1
    assert digest == evidence_events[0].payload["evidence_digest"]
