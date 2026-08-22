"""Verifier-triggered, bounded model escalation contracts.

The policy is deliberately independent of providers and persistence.  A caller
must declare the routes permitted for the request; a failed verifier or high
uncertainty may move only to the next declared route, and every decision can be
closed with an outcome recording whether the stronger route helped.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


# These are adapter-independent safety ceilings.  A request may choose a
# smaller budget, but it cannot enlarge the runtime's bounded escalation
# envelope through untrusted request data.
MAX_ESCALATIONS = 2
MAX_ROUTES = 8
MAX_ID_CHARS = 128
MAX_MODEL_CHARS = 256
MAX_REASON_CHARS = 128
MAX_PROVENANCE_ITEMS = 8
MAX_PROVENANCE_CHARS = 256


class EscalationReason(str, Enum):
    """The only verifier-facing conditions that may authorize escalation."""

    UNCERTAINTY = "high_uncertainty"
    VERIFIER_FAILURE = "verifier_failure"


def _bounded_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _provenance(value: Iterable[str], *, required: bool = False) -> tuple[str, ...]:
    try:
        items = tuple(value)
    except TypeError as exc:
        raise ValueError("provenance must be a bounded tuple of strings") from exc
    if len(items) > MAX_PROVENANCE_ITEMS or any(
        not isinstance(item, str) or not item.strip() or len(item) > MAX_PROVENANCE_CHARS
        for item in items
    ):
        raise ValueError("provenance must be a bounded tuple of strings")
    if required and not items:
        raise ValueError("escalation evidence provenance is required")
    return tuple(item.strip() for item in items)


@dataclass(frozen=True)
class EscalationRoute:
    route_id: str
    model: str
    rank: int

    def __post_init__(self) -> None:
        _bounded_text(self.route_id, "route_id", MAX_ID_CHARS)
        _bounded_text(self.model, "model", MAX_MODEL_CHARS)
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 0:
            raise ValueError("route rank must be a non-negative integer")
        object.__setattr__(self, "route_id", self.route_id.strip())
        object.__setattr__(self, "model", self.model.strip())


@dataclass(frozen=True)
class EscalationRequest:
    request_id: str
    permitted_routes: tuple[EscalationRoute, ...]
    current_route_id: str
    uncertainty: float = 0.0
    verifier_passed: bool | None = None
    escalation_count: int = 0
    max_escalations: int = 1
    uncertainty_threshold: float = 0.65
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.request_id, "request_id", MAX_ID_CHARS)
        _bounded_text(self.current_route_id, "current_route_id", MAX_ID_CHARS)
        if not 0.0 <= self.uncertainty <= 1.0:
            raise ValueError("uncertainty must be between 0 and 1")
        if (isinstance(self.escalation_count, bool) or not isinstance(self.escalation_count, int)
                or self.escalation_count < 0 or self.escalation_count > MAX_ESCALATIONS):
            raise ValueError("escalation_count is outside the bounded policy")
        if (isinstance(self.max_escalations, bool) or not isinstance(self.max_escalations, int)
                or self.max_escalations < 0 or self.max_escalations > MAX_ESCALATIONS):
            raise ValueError("max_escalations is outside the bounded policy")
        if not 0.0 <= self.uncertainty_threshold <= 1.0:
            raise ValueError("uncertainty threshold must be between 0 and 1")
        routes = tuple(self.permitted_routes)
        if not routes or len(routes) > MAX_ROUTES:
            raise ValueError("at least one permitted route is required")
        if len({route.route_id for route in routes}) != len(routes):
            raise ValueError("permitted routes must have unique identities")
        if len({route.rank for route in routes}) != len(routes):
            raise ValueError("permitted routes must have unique ranks")
        if self.current_route_id not in {route.route_id for route in routes}:
            raise ValueError("current route must be explicitly permitted")
        object.__setattr__(self, "permitted_routes", routes)
        object.__setattr__(self, "provenance", _provenance(self.provenance))
        object.__setattr__(self, "request_id", self.request_id.strip())
        object.__setattr__(self, "current_route_id", self.current_route_id.strip())


@dataclass(frozen=True)
class EscalationDecision:
    request_id: str
    selected_route: EscalationRoute
    escalated: bool
    trigger: str
    escalation_count: int
    can_escalate: bool
    reason: EscalationReason | None = None
    provenance: tuple[str, ...] = ()
    denied: bool = False


@dataclass(frozen=True)
class EscalationOutcome:
    request_id: str
    from_route_id: str
    to_route_id: str
    helped: bool
    evidence: str
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.request_id, "request_id", MAX_ID_CHARS)
        _bounded_text(self.from_route_id, "from_route_id", MAX_ID_CHARS)
        _bounded_text(self.to_route_id, "to_route_id", MAX_ID_CHARS)
        _bounded_text(self.evidence, "evidence", MAX_PROVENANCE_CHARS)
        if not self.request_id.strip() or not self.from_route_id.strip() or not self.to_route_id.strip():
            raise ValueError("outcome route identities are required")
        object.__setattr__(self, "provenance", _provenance(self.provenance, required=True))


@dataclass(frozen=True)
class ControlledEscalationPolicy:
    """Immutable, bounded policy shared by the pure decision and service."""

    max_escalations: int = MAX_ESCALATIONS
    uncertainty_threshold: float = 0.65

    def __post_init__(self) -> None:
        if (isinstance(self.max_escalations, bool) or not isinstance(self.max_escalations, int)
                or not 0 <= self.max_escalations <= MAX_ESCALATIONS):
            raise ValueError("max_escalations is outside the bounded policy")
        if not 0.0 <= self.uncertainty_threshold <= 1.0:
            raise ValueError("uncertainty threshold must be between 0 and 1")

    def request(self, request: EscalationRequest) -> EscalationRequest:
        """Apply policy defaults while preserving request-scoped permissions."""
        if request.max_escalations > self.max_escalations:
            raise ValueError("request escalation budget exceeds the configured policy")
        if request.uncertainty_threshold != self.uncertainty_threshold:
            raise ValueError("request uncertainty threshold does not match the policy")
        return request


class ControlledEscalation:
    """Plan one bounded escalation using only request-declared routes."""

    def __init__(self, policy: ControlledEscalationPolicy | None = None) -> None:
        self.policy = policy or ControlledEscalationPolicy()

    def decide(self, request: EscalationRequest) -> EscalationDecision:
        self.policy.request(request)
        routes = tuple(sorted(request.permitted_routes, key=lambda route: (route.rank, route.route_id)))
        current_index = next(index for index, route in enumerate(routes) if route.route_id == request.current_route_id)
        reason = self._trigger(request)
        evidence_ready = bool(request.provenance)
        can_move = bool(reason) and evidence_ready and request.escalation_count < request.max_escalations and current_index < len(routes) - 1
        selected = routes[current_index + 1] if can_move else routes[current_index]
        count = request.escalation_count + 1 if can_move else request.escalation_count
        denied = bool(reason) and not can_move
        return EscalationDecision(
            request_id=request.request_id,
            selected_route=selected,
            escalated=can_move,
            trigger=reason.value if can_move else (
                "missing_provenance" if reason and not evidence_ready else
                "escalation_limit" if reason else "no_escalation"
            ),
            escalation_count=count,
            can_escalate=count < request.max_escalations and current_index + (1 if can_move else 0) < len(routes) - 1,
            reason=reason,
            provenance=request.provenance,
            denied=denied,
        )

    @staticmethod
    def _trigger(request: EscalationRequest) -> EscalationReason | None:
        if request.verifier_passed is False:
            return EscalationReason.VERIFIER_FAILURE
        if request.uncertainty >= request.uncertainty_threshold:
            return EscalationReason.UNCERTAINTY
        return None

    @staticmethod
    def record_outcome(request: EscalationRequest, decision: EscalationDecision, *, helped: bool, evidence: str) -> EscalationOutcome:
        if not decision.escalated or decision.reason is None:
            raise ValueError("only an escalation decision can record an escalation outcome")
        if decision.request_id != request.request_id:
            raise ValueError("decision does not belong to request")
        return EscalationOutcome(
            request.request_id, request.current_route_id,
            decision.selected_route.route_id, helped, evidence,
            provenance=decision.provenance,
        )


class ControlledEscalationService:
    """Application boundary for decisions and auditable escalation outcomes."""

    def __init__(self, policy: ControlledEscalationPolicy | None = None, event_sink=None) -> None:
        self._policy = ControlledEscalation(policy)
        self._event_sink = event_sink

    def decide(self, request: EscalationRequest) -> EscalationDecision:
        decision = self._policy.decide(request)
        if self._event_sink is not None:
            self._event_sink.emit(
                "model.escalation.decided",
                summary="bounded model escalation evaluated",
                detail={
                    "request_id": request.request_id,
                    "from_route": request.current_route_id,
                    "to_route": decision.selected_route.route_id,
                    "trigger": decision.trigger,
                    "escalated": decision.escalated,
                    "denied": decision.denied,
                },
                operation_id=request.request_id,
            )
        return decision

    def record_outcome(
        self, request: EscalationRequest, decision: EscalationDecision,
        *, helped: bool, evidence: str,
    ) -> EscalationOutcome:
        outcome = self._policy.record_outcome(request, decision, helped=helped, evidence=evidence)
        if self._event_sink is not None:
            self._event_sink.emit(
                "model.escalation.outcome",
                summary="bounded model escalation outcome recorded",
                detail={"request_id": outcome.request_id, "helped": outcome.helped},
                operation_id=outcome.request_id,
            )
        return outcome


__all__ = [
    "ControlledEscalation", "ControlledEscalationPolicy", "ControlledEscalationService",
    "EscalationDecision", "EscalationOutcome", "EscalationReason", "EscalationRequest",
    "EscalationRoute", "MAX_ESCALATIONS", "MAX_ROUTES",
]
