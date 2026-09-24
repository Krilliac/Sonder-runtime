"""Production composition binds EVAL lifecycle/gates to host-owned ports."""

import pytest

from sonder_runtime.adapters.evaluation_corpus import EvaluationCorpusSource
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.evaluation.corpus_inventory import (
    CorpusSourceKind, CorpusSourceSpec, EvaluationCorpusCoverageError,
)
from sonder_runtime.application.evaluation.promotion_gates import PromotionKind
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension, EvaluationLifecycleError, EvaluationSuite, ProposalState,
)
from sonder_runtime.bootstrap.evaluation import compose_evaluation_service


SUITE = EvaluationSuite(
    "strategy-orchestration", "strategy-v1",
    (EvaluationDimension("split", "heldout"),), ("pass_rate",),
)


def test_host_composition_requires_real_corpus_coverage_before_evaluation(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = compose_evaluation_service(repo, failure_directory=tmp_path / "failures")
    proposal = service.create_proposal(
        "strategy-1", "candidate", "baseline", SUITE, kind=PromotionKind.STRATEGY,
    )
    assert proposal.promotion_kind == "strategy"
    assert service.submit(proposal.proposal_id).state is ProposalState.SUBMITTED
    with pytest.raises(EvaluationLifecycleError, match="bound promotion kind"):
        service.create_proposal("ungated", "candidate", "baseline", SUITE)
    with pytest.raises(EvaluationLifecycleError, match="ungated production"):
        service.promotion_evidence("strategy-1", gate_results={"fake": True},
                                   replay_equivalent=True, holdout_passed=True,
                                   rollback_reference="baseline", provenance=("fake",))
    with pytest.raises(EvaluationCorpusCoverageError, match="incomplete"):
        service.begin_evaluation(proposal.proposal_id)
    assert [event.event_type for event in repo.read_range("evaluation:strategy-1")] == [
        "evaluation.proposal.created", "evaluation.proposal.submitted",
    ]


def test_host_composition_begins_after_bounded_repository_tool_memory_scans(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    sources = tuple(
        EvaluationCorpusSource(
            CorpusSourceSpec(kind.value, kind),
            lambda **_limit: ({"id": "measured-record"},),
        ) for kind in CorpusSourceKind
    )
    service = compose_evaluation_service(repo, sources=sources)
    proposal = service.create_proposal(
        "strategy-2", "candidate", "baseline", SUITE, kind=PromotionKind.STRATEGY,
    )
    service.submit(proposal.proposal_id)
    assert service.inventory(require_complete=True).complete
    assert service.begin_evaluation(proposal.proposal_id).state is ProposalState.EVALUATING
    assert repo.read_range("evaluation:strategy-2")[-1].event_type == "evaluation.proposal.evaluating"


def test_application_graph_exposes_one_lazy_fail_closed_strategy_gate(tmp_path):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    app = build_application(config=SonderConfig(state=StateConfig(home=str(tmp_path))))
    try:
        assert callable(app.evaluation_service)
        service = app.evaluation_service()
        assert app.evaluation_service() is service
        assert not service.inventory().complete
        assert service.create_proposal(
            "runtime-strategy", "candidate", "baseline", SUITE,
            kind=PromotionKind.STRATEGY,
        ).promotion_kind == "strategy"
    finally:
        app.close_providers(timeout=2)


def test_changed_complete_corpus_invalidates_a_live_evaluation(tmp_path):
    revision = {"value": "first"}
    sources = tuple(EvaluationCorpusSource(
        CorpusSourceSpec(kind.value, kind),
        lambda **_limit: ({"id": revision["value"]},),
    ) for kind in CorpusSourceKind)
    service = compose_evaluation_service(
        SQLiteSessionRepository(tmp_path / "sessions.db"), sources=sources,
    )
    proposal = service.create_proposal(
        "corpus-revision", "candidate", "baseline", SUITE,
        kind=PromotionKind.STRATEGY,
    )
    service.submit(proposal.proposal_id)
    service.begin_evaluation(proposal.proposal_id)
    original = service.inventory(require_complete=True).digest
    revision["value"] = "second"
    assert service.inventory(require_complete=True).digest != original
    with pytest.raises(EvaluationLifecycleError, match="corpus changed"):
        service.begin_shadow(proposal.proposal_id)
    with pytest.raises(EvaluationLifecycleError, match="corpus changed"):
        service.gated_promotion_evidence(
            proposal.proposal_id, baseline_pass_rate=1.0, case_regressions=0,
            holdout_passed=True, rollback_reference="baseline", provenance=("source",),
        )
