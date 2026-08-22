"""Deterministic capability-based role routing and bounded escalation."""
from __future__ import annotations

from dataclasses import dataclass

from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit
from sonder_runtime.domain.routing.capability_profiles import (
    Capability,
    CapabilityProfile,
    RoleRoute,
    default_role_routes,
)


@dataclass(frozen=True)
class RoutingRequest:
    role: AgentRole
    required: frozenset[Capability] = frozenset()
    uncertainty: float = 0.0
    verifier_passed: bool | None = None
    escalation_count: int = 0
    requested_budget: BudgetLimit | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")
        if self.requested_budget is not None and not isinstance(
            self.requested_budget, BudgetLimit
        ):
            raise ValueError("requested_budget must be a BudgetLimit")
        if not 0.0 <= self.uncertainty <= 1.0:
            raise ValueError("uncertainty must be between 0 and 1")
        if self.escalation_count < 0:
            raise ValueError("escalation_count must be non-negative")
        object.__setattr__(self, "required", frozenset(self.required))


@dataclass(frozen=True)
class RouteDecision:
    role: AgentRole
    model: str
    profile: CapabilityProfile
    budget: BudgetLimit
    escalated: bool
    reason: str
    escalation_count: int
    can_escalate: bool


class CapabilityRouter:
    """Select a measured profile without model, network, or persistence I/O."""

    def __init__(
        self,
        profiles: tuple[CapabilityProfile, ...],
        *,
        role_routes: dict[AgentRole, RoleRoute] | None = None,
        max_escalations: int = 2,
        uncertainty_threshold: float = 0.65,
    ) -> None:
        if max_escalations < 0 or not 0.0 <= uncertainty_threshold <= 1.0:
            raise ValueError("invalid escalation policy")
        if not profiles:
            raise ValueError("at least one capability profile is required")
        self._profiles = tuple(profiles)
        self._roles = dict(role_routes or default_role_routes())
        self._max_escalations = max_escalations
        self._uncertainty_threshold = uncertainty_threshold

    def route(self, request: RoutingRequest) -> RouteDecision:
        try:
            role_route = self._roles[request.role]
        except KeyError as exc:
            raise ValueError(f"no route policy for role {request.role.value}") from exc
        required = role_route.required | request.required
        self._validate_budget(request.requested_budget, role_route.budget)
        candidates = [p for p in self._profiles if p.supports(required)]
        if not candidates:
            raise ValueError("no profile satisfies requested capabilities")
        candidates.sort(key=lambda p: (p.escalation_rank, -p.quality, p.latency_ms, p.model))
        index = min(request.escalation_count, len(candidates) - 1)
        trigger = self._trigger(request)
        if trigger and request.escalation_count < self._max_escalations:
            index = min(index + 1, len(candidates) - 1)
        profile = candidates[index]
        count = max(request.escalation_count, index)
        escalated = count > request.escalation_count
        reason = trigger if escalated else ("initial" if not trigger else "escalation_limit")
        return RouteDecision(
            role=request.role, model=profile.model, profile=profile,
            budget=role_route.budget, escalated=escalated, reason=reason,
            escalation_count=count,
            can_escalate=count < self._max_escalations and index < len(candidates) - 1,
        )

    @staticmethod
    def _validate_budget(
        requested: BudgetLimit | None, allowed: BudgetLimit
    ) -> None:
        """Reject a caller ceiling wider than the immutable role ceiling.

        A route is an admission decision, so an over-budget request must fail
        before a model is selected or handed to a provider.  ``None`` means
        that the caller did not widen the role's existing limit.
        """
        if requested is None:
            return
        for field in ("steps", "output_tokens", "wall_seconds"):
            requested_value = getattr(requested, field)
            allowed_value = getattr(allowed, field)
            if requested_value is None:
                continue
            if allowed_value is None or requested_value > allowed_value:
                raise ValueError(
                    f"requested {field} budget exceeds immutable role budget"
                )

    def _trigger(self, request: RoutingRequest) -> str | None:
        if request.verifier_passed is False:
            return "verifier_failure"
        if request.uncertainty >= self._uncertainty_threshold:
            return "high_uncertainty"
        return None


__all__ = ["CapabilityRouter", "RouteDecision", "RoutingRequest"]
