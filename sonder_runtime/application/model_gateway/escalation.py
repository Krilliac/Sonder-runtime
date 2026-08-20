"""Verifier-triggered, bounded model escalation contracts.

The policy is deliberately independent of providers and persistence.  A caller
must declare the routes permitted for the request; a failed verifier or high
uncertainty may move only to the next declared route, and every decision can be
closed with an outcome recording whether the stronger route helped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class EscalationRoute:
    route_id: str
    model: str
    rank: int

    def __post_init__(self) -> None:
        if not self.route_id.strip() or not self.model.strip() or self.rank < 0:
            raise ValueError("route identity and non-negative rank are required")


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

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.current_route_id.strip():
            raise ValueError("request and current route identities are required")
        if not 0.0 <= self.uncertainty <= 1.0:
            raise ValueError("uncertainty must be between 0 and 1")
        if self.escalation_count < 0 or self.max_escalations < 0:
            raise ValueError("escalation counts must be non-negative")
        if not 0.0 <= self.uncertainty_threshold <= 1.0:
            raise ValueError("uncertainty threshold must be between 0 and 1")
        routes = tuple(self.permitted_routes)
        if not routes:
            raise ValueError("at least one permitted route is required")
        if len({route.route_id for route in routes}) != len(routes):
            raise ValueError("permitted routes must have unique identities")
        if self.current_route_id not in {route.route_id for route in routes}:
            raise ValueError("current route must be explicitly permitted")
        object.__setattr__(self, "permitted_routes", routes)


@dataclass(frozen=True)
class EscalationDecision:
    request_id: str
    selected_route: EscalationRoute
    escalated: bool
    trigger: str
    escalation_count: int
    can_escalate: bool


@dataclass(frozen=True)
class EscalationOutcome:
    request_id: str
    from_route_id: str
    to_route_id: str
    helped: bool
    evidence: str

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.from_route_id.strip() or not self.to_route_id.strip():
            raise ValueError("outcome route identities are required")
        if not self.evidence.strip():
            raise ValueError("escalation outcome requires evidence")


class ControlledEscalation:
    """Plan one bounded escalation using only request-declared routes."""

    def decide(self, request: EscalationRequest) -> EscalationDecision:
        routes = tuple(sorted(request.permitted_routes, key=lambda route: (route.rank, route.route_id)))
        current_index = next(index for index, route in enumerate(routes) if route.route_id == request.current_route_id)
        trigger = self._trigger(request)
        can_move = bool(trigger) and request.escalation_count < request.max_escalations and current_index < len(routes) - 1
        selected = routes[current_index + 1] if can_move else routes[current_index]
        count = request.escalation_count + 1 if can_move else request.escalation_count
        return EscalationDecision(
            request_id=request.request_id,
            selected_route=selected,
            escalated=can_move,
            trigger=trigger if can_move else ("escalation_limit" if trigger else "no_escalation"),
            escalation_count=count,
            can_escalate=count < request.max_escalations and current_index + (1 if can_move else 0) < len(routes) - 1,
        )

    @staticmethod
    def _trigger(request: EscalationRequest) -> str | None:
        if request.verifier_passed is False:
            return "verifier_failure"
        if request.uncertainty >= request.uncertainty_threshold:
            return "high_uncertainty"
        return None

    @staticmethod
    def record_outcome(request: EscalationRequest, decision: EscalationDecision, *, helped: bool, evidence: str) -> EscalationOutcome:
        if not decision.escalated:
            raise ValueError("only an escalation decision can record an escalation outcome")
        return EscalationOutcome(request.request_id, request.current_route_id, decision.selected_route.route_id, helped, evidence)


__all__ = ["ControlledEscalation", "EscalationDecision", "EscalationOutcome", "EscalationRequest", "EscalationRoute"]
