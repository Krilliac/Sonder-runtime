"""Host-owned learning persistence at the managed terminal verifier boundary.

The recorder is composed by bootstrap with the owned ``Application`` and the
private standalone verifier factory.  Callers hand it the managed owner and
the terminal eligibility decision that owner's verifier boundary just issued;
they never supply receipt, worker, trust, or fact text.  The receipt producer
reads only the decision sealed inside the boundary-issued authority, so the
boundary is resolved once (no second publication or manifest capture) and
modified public fields on a copy are ignored.

Learning is fail-closed and never changes the outward work result: a refused
or failed persistence records a bounded outcome with its reason and leaves no
observation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
from threading import Lock

from ..application.ports.terminal_eligibility import (
    ManagedTerminalEligibility,
    _HostVerifierAuthority,
)


_LOG = logging.getLogger(__name__)
_LEARNING_PHASES = frozenset({"certified", "certified_after_return", "failed"})
_REASON_LIMIT = 256


def _reason(error):
    text = "%s: %s" % (type(error).__name__, error)
    return text[:_REASON_LIMIT]


@dataclass(frozen=True)
class ManagedLearningOutcome:
    status: str  # not_applicable | persisted | refused
    code: str
    observation_id: str | None = None
    promotion: str | None = None
    reason: str = ""


class ManagedLearningRecorder:
    """Persist authenticated verifier observations for one owned Application."""

    def __init__(self, application, *, verifier_factory, history=64):
        if application is None or not callable(verifier_factory):
            raise TypeError("owned application and private verifier factory required")
        self._application = application
        self._verifier_factory = verifier_factory
        self._lock = Lock()
        self._recent = deque(maxlen=history)

    def recent(self):
        with self._lock:
            return tuple(self._recent)

    def _remember(self, outcome):
        with self._lock:
            self._recent.append(outcome)
        if outcome.status == "refused" or outcome.promotion == "refused":
            # Structured, bounded refusal record; never carries model text.
            _LOG.warning(
                "managed learning refused status=%s code=%s promotion=%s reason=%s",
                outcome.status, outcome.code, outcome.promotion, outcome.reason,
                extra={"sonder_learning_outcome": outcome},
            )
        return outcome

    def __call__(self, owner, expected_turn, eligibility):
        return self._remember(self._record(owner, expected_turn, eligibility))

    def _record(self, owner, expected_turn, eligibility):
        from .managed_conversation import ManagedConversationLifetime
        from .managed_standalone import ManagedStandaloneSession

        if (
            type(eligibility) is not ManagedTerminalEligibility
            or type(eligibility.authority) is not _HostVerifierAuthority
            or eligibility.phase not in _LEARNING_PHASES
        ):
            # Only decisions issued by the managed verifier boundary carry an
            # authority; everything else is never learning evidence.
            return ManagedLearningOutcome("not_applicable", "NO_VERIFIER_AUTHORITY")
        if type(owner) not in (ManagedConversationLifetime, ManagedStandaloneSession):
            return ManagedLearningOutcome("refused", "MANAGED_OWNER_REQUIRED")
        try:
            observation = owner.persist_learning_observation_durable(
                expected_turn,
                verifier_factory=self._verifier_factory,
                eligibility=eligibility,
            )
        except Exception as error:  # fail closed; never alter the work result
            return ManagedLearningOutcome(
                "refused",
                "PERSIST_" + type(error).__name__.upper(),
                reason=_reason(error),
            )
        promotion, reason = self._promote(observation)
        return ManagedLearningOutcome(
            "persisted",
            "OBSERVATION_PERSISTED",
            observation.observation_id,
            promotion,
            reason,
        )

    def _promote(self, observation):
        """Run the verified-subject ladder only through the composed fact source."""
        from ..application.memory.receipt_observation import (
            VerifiedSubjectFactPromotion,
        )

        memory = getattr(self._application, "memory", None)
        unit_of_work = getattr(self._application, "unit_of_work", None)
        if not callable(getattr(memory, "promote_verified_subject", None)) or not callable(
            unit_of_work
        ):
            return "unconfigured", ""
        try:
            with unit_of_work() as scope:
                source = scope.authoritative_fact_source
                pair = scope.verifier_observations.get(observation.observation_id)
            if source is None:
                return "unconfigured", ""
            if pair is None or pair[1] != observation:
                return "refused", "persisted observation is not visible"
            project = pair[0].project_scope
            if project != getattr(source, "project_scope", None):
                # The fact source only owns its configured project scope.
                return "out_of_scope", ""
            status, _decision = memory.promote_verified_subject(
                project,
                VerifiedSubjectFactPromotion.fact_id_for_subject(observation.content),
                (observation.observation_id,),
            )
            return status, ""
        except Exception as error:
            return "refused", _reason(error)


__all__ = ["ManagedLearningOutcome", "ManagedLearningRecorder"]
