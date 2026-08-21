from __future__ import annotations

import pytest

from sonder_runtime.adapters.evaluation_corpus import BoundedEvaluationCorpusScanner, EvaluationCorpusSource
from sonder_runtime.application.evaluation.service import EvaluationApplicationService
from sonder_runtime.application.evaluation.corpus_inventory import (
    CorpusSourceKind,
    CorpusSourceSpec,
    EvaluationCorpusCoverageError,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
    ProposalLifecycle,
    ProposalState,
    ShadowCanaryObservation,
)
from sonder_runtime.application.evaluation.trajectory_replay import TrajectoryRecord, TrajectoryStep


def _source(kind: CorpusSourceKind, source_id: str):
    return EvaluationCorpusSource(CorpusSourceSpec(source_id, kind), lambda **_: [{"id": source_id}])


def _suite() -> EvaluationSuite:
    return EvaluationSuite("quality", "v1", (EvaluationDimension("split", "holdout"),), ("accuracy",))


def _result(suite: EvaluationSuite, result_id: str, mode: EvaluationMode) -> EvaluationResult:
    return EvaluationResult(
        result_id, suite, "candidate", "baseline", mode, suite.dimensions,
        {"accuracy": 1.0}, True, 1, replay_equivalent=True, provenance=("test",),
    )


def _service(*, complete: bool = True) -> EvaluationApplicationService:
    sources = [_source(CorpusSourceKind.REPOSITORY, "repo"), _source(CorpusSourceKind.TOOL, "tool")]
    if complete:
        sources.append(_source(CorpusSourceKind.MEMORY, "memory"))
    return EvaluationApplicationService(
        corpus=BoundedEvaluationCorpusScanner(sources), lifecycle=ProposalLifecycle(),
    )


def test_boundary_composes_suite_inventory_and_provider_neutral_replay() -> None:
    service = _service()
    suite = service.register_suite(_suite())
    assert service.resolve_suite("quality", "v1") == suite
    assert service.inventory(require_complete=True).complete

    expected = TrajectoryRecord.from_steps("t1", (TrajectoryStep(0, {"x": 1}, {"y": 2}),))
    report = service.replay(expected, lambda value: {"y": value["x"] + 1})
    assert report.equivalent
    assert report.expected_digest == expected.digest


def test_incomplete_inventory_is_exposed_and_can_fail_closed() -> None:
    service = _service(complete=False)
    assert not service.inventory().complete
    with pytest.raises(EvaluationCorpusCoverageError, match="incomplete"):
        service.inventory(require_complete=True)


def test_boundary_exposes_explicit_shadow_canary_evidence_and_attended_promotion() -> None:
    service = _service()
    suite = service.register_suite(_suite())
    service.create_proposal("p1", "candidate", "baseline", suite)
    service.submit("p1")
    service.begin_evaluation("p1")
    service.record_result("p1", _result(suite, "offline", EvaluationMode.OFFLINE))
    service.begin_shadow("p1")
    service.record_observation("p1", ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 1, {"error": 0}, 0))
    service.begin_canary("p1")
    service.record_result("p1", _result(suite, "canary", EvaluationMode.CANARY))
    service.record_observation("p1", ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 1, {"error": 0}, .1))
    evidence = service.promotion_evidence(
        "p1", gate_results={"quality": True}, replay_equivalent=True,
        holdout_passed=True, rollback_reference="baseline", provenance=("test",),
    )
    assert evidence.accepted
    assert service.approve("p1", evidence.digest).state is ProposalState.READY_FOR_PROMOTION
    with pytest.raises(ValueError, match="attended"):
        service.promote("p1", evidence.digest)
    assert service.promote("p1", evidence.digest, attended=True).state is ProposalState.PROMOTED

