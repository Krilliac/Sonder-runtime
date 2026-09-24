"""Host composition for proposal evaluation, deliberately fail-closed on corpus coverage.

Suites and proposal decisions live in one application instance. Events and
minimized failures are durable, but proposal state is not rehydrated from those
events on restart; a restored proposal cannot be approved until that contract
exists. The host supplies real bounded corpus readers before running an eval.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sonder_runtime.adapters.evaluation_corpus import (
    BoundedEvaluationCorpusScanner,
    EvaluationCorpusSource,
)
from sonder_runtime.adapters.evaluation_failure_corpus import JsonMinimizedFailureStore
from sonder_runtime.adapters.evaluation_lifecycle import (
    SessionEvaluationLifecycleRepository,
)
from sonder_runtime.application.evaluation.durable_lifecycle import (
    EvaluationLifecycleService,
)
from sonder_runtime.application.evaluation.promotion_gates import (
    PromotionGateEvaluator,
    PromotionKind,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationLifecycleError,
    EvaluationResult,
    ProposalLifecycle,
    ShadowCanaryObservation,
)
from sonder_runtime.application.evaluation.service import EvaluationApplicationService
from sonder_runtime.application.evaluation.strategy_cases import (
    StrategyEvidenceClass,
    strategy_evidence_class,
)
from sonder_runtime.application.ports.session_repository import SessionRepository
from sonder_runtime.domain.strategy.models import StrategyError


class HostEvaluationService(EvaluationApplicationService):
    """A candidate cannot begin evaluation or receive evidence without coverage."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._corpus_at_begin: dict[str, str] = {}

    def _require_current_corpus(self, proposal_id: str) -> None:
        recorded = self._corpus_at_begin.get(proposal_id)
        current = self.inventory(require_complete=True).digest
        if recorded is None or current != recorded:
            raise EvaluationLifecycleError("evaluation corpus changed since the proposal began")

    def create_proposal(self, proposal_id, candidate, baseline, suite, *, kind=None):
        if not isinstance(kind, PromotionKind):
            raise EvaluationLifecycleError("production evaluation requires a bound promotion kind")
        return super().create_proposal(proposal_id, candidate, baseline, suite, kind=kind)

    def begin_evaluation(self, proposal_id):
        inventory = self.inventory(require_complete=True)
        proposal = super().begin_evaluation(proposal_id)
        self._corpus_at_begin[proposal_id] = inventory.digest
        return proposal

    def record_result(self, proposal_id: str, result: EvaluationResult) -> EvaluationResult:
        self._require_current_corpus(proposal_id)
        proposal = self._lifecycle.lifecycle.get(proposal_id)
        if proposal.promotion_kind == PromotionKind.STRATEGY.value:
            try:
                evidence_class = strategy_evidence_class(result)
            except StrategyError as error:
                raise EvaluationLifecycleError("strategy result needs a typed host evidence class") from error
            if evidence_class is not StrategyEvidenceClass.SYNTHETIC_POLICY_CANARY:
                raise EvaluationLifecycleError("real task receipts have no configured host validator")
            if any(result.metrics.get(name) != 0.0 for name in (
                "task_success", "first_attempt_success", "repair_success",
            )):
                raise EvaluationLifecycleError("synthetic strategy case cannot claim measured task success")
        return super().record_result(proposal_id, result)

    def record_observation(self, proposal_id: str,
                           observation: ShadowCanaryObservation) -> ShadowCanaryObservation:
        self._require_current_corpus(proposal_id)
        return super().record_observation(proposal_id, observation)

    def begin_shadow(self, proposal_id):
        self._require_current_corpus(proposal_id)
        return super().begin_shadow(proposal_id)

    def begin_canary(self, proposal_id):
        self._require_current_corpus(proposal_id)
        return super().begin_canary(proposal_id)

    def promotion_gate_decision(self, proposal_id, *, baseline_pass_rate, case_regressions):
        self._require_current_corpus(proposal_id)
        return super().promotion_gate_decision(
            proposal_id, baseline_pass_rate=baseline_pass_rate,
            case_regressions=case_regressions,
        )

    def promotion_evidence(self, *args, **kwargs):
        raise EvaluationLifecycleError("ungated production promotion evidence is unavailable")

    def gated_promotion_evidence(self, proposal_id, **kwargs):
        self._require_current_corpus(proposal_id)
        return super().gated_promotion_evidence(proposal_id, **kwargs)

    def approve(self, proposal_id, evidence_digest, *, allow_ungated_legacy=False):
        self._require_current_corpus(proposal_id)
        if allow_ungated_legacy:
            raise EvaluationLifecycleError("ungated production approval is unavailable")
        return super().approve(proposal_id, evidence_digest)

    def promote(self, proposal_id, evidence_digest, *, attended=False):
        self._require_current_corpus(proposal_id)
        return super().promote(proposal_id, evidence_digest, attended=attended)


def compose_evaluation_service(
    repository: SessionRepository,
    *,
    sources: Iterable[EvaluationCorpusSource] = (),
    failure_directory: str | Path | None = None,
) -> HostEvaluationService:
    """Bind canonical per-kind gates and durable host ports to an application."""
    if repository is None:
        raise TypeError("evaluation session repository is required")
    lifecycle = EvaluationLifecycleService(
        ProposalLifecycle(promotion_gate=PromotionGateEvaluator()),
        SessionEvaluationLifecycleRepository(repository),
    )
    failures = (
        JsonMinimizedFailureStore(failure_directory)
        if failure_directory is not None else None
    )
    return HostEvaluationService(
        corpus=BoundedEvaluationCorpusScanner(sources),
        lifecycle=lifecycle, failures=failures,
    )


__all__ = ["HostEvaluationService", "compose_evaluation_service"]
