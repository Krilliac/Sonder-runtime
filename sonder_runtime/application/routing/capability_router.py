"""Deterministic capability-based role routing and bounded escalation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendIdentity,
    EvidenceState,
    backend_requirements,
)
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
    previous_model: str | None = None
    spent_tokens: int = 0
    spent_wall_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")  # noqa: TRY004 - established route API
        if self.requested_budget is not None and not isinstance(
            self.requested_budget, BudgetLimit
        ):
            raise ValueError("requested_budget must be a BudgetLimit")
        if not 0.0 <= self.uncertainty <= 1.0:
            raise ValueError("uncertainty must be between 0 and 1")
        if self.escalation_count < 0:
            raise ValueError("escalation_count must be non-negative")
        if (self.previous_model is not None and (
            type(self.previous_model) is not str or not self.previous_model.strip()
        )):
            raise ValueError("previous_model must identify a concrete prior model")
        if type(self.spent_tokens) is not int or self.spent_tokens < 0:
            raise ValueError("spent_tokens must be non-negative")
        if (not math.isfinite(self.spent_wall_seconds)
                or self.spent_wall_seconds < 0):
            raise ValueError("spent_wall_seconds must be non-negative and finite")
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
    evidence_reason: str = ""
    prior_model: str | None = None
    spent_tokens: int = 0
    spent_wall_seconds: float = 0.0

    def attribute_outcome(self, *, verifier_passed: bool | None,
                          tokens_used: int, wall_seconds: float) -> RouteOutcome:
        """Record actual verifier lift and cost; route choice is not success."""
        if verifier_passed is not None and type(verifier_passed) is not bool:
            raise ValueError("verifier_passed must be boolean or unknown")
        if type(tokens_used) is not int or tokens_used < 0 or not math.isfinite(wall_seconds) or wall_seconds < 0:
            raise ValueError("route outcome resources must be non-negative")
        return RouteOutcome(
            model=self.model, prior_model=self.prior_model, trigger=self.reason,
            verifier_passed=verifier_passed,
            helped=(verifier_passed if self.reason == "verifier_failure" else None),
            total_tokens=self.spent_tokens + tokens_used,
            total_wall_seconds=self.spent_wall_seconds + wall_seconds,
            within_budget=(self.budget.output_tokens is None
                           or tokens_used <= self.budget.output_tokens)
            and (self.budget.wall_seconds is None
                 or wall_seconds <= self.budget.wall_seconds),
        )


@dataclass(frozen=True)
class RouteOutcome:
    model: str
    prior_model: str | None
    trigger: str
    verifier_passed: bool | None
    helped: bool | None
    total_tokens: int
    total_wall_seconds: float
    within_budget: bool


class CapabilityRoutingError(ValueError):
    """A route was refused with a machine-readable admission reason."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class CapabilityRouter:
    """Select a measured profile without model, network, or persistence I/O."""

    def __init__(
        self,
        profiles: tuple[CapabilityProfile, ...],
        *,
        role_routes: dict[AgentRole, RoleRoute] | None = None,
        max_escalations: int = 2,
        uncertainty_threshold: float = 0.65,
        recent_evidence=None,
        evidence_clock=None,
        identity_for=None,
    ) -> None:
        if max_escalations < 0 or not 0.0 <= uncertainty_threshold <= 1.0:
            raise ValueError("invalid escalation policy")
        if not profiles:
            raise ValueError("at least one capability profile is required")
        if identity_for is not None and recent_evidence is None:
            raise ValueError("identity-bound routing requires recent evidence")
        self._profiles = tuple(profiles)
        self._roles = dict(role_routes or default_role_routes())
        self._max_escalations = max_escalations
        self._uncertainty_threshold = uncertainty_threshold
        self._recent_evidence = recent_evidence
        self._evidence_clock = evidence_clock
        self._identity_for = identity_for

    def route(self, request: RoutingRequest) -> RouteDecision:
        try:
            role_route = self._roles[request.role]
        except KeyError as exc:
            raise ValueError(f"no route policy for role {request.role.value}") from exc
        required = role_route.required | request.required
        self._validate_budget(request.requested_budget, role_route.budget)
        if self._identity_for is not None and (
            role_route.budget.output_tokens is not None
            and request.spent_tokens >= role_route.budget.output_tokens
            or role_route.budget.wall_seconds is not None
            and request.spent_wall_seconds >= role_route.budget.wall_seconds
        ):
            raise CapabilityRoutingError("route_budget_exhausted", "role resource budget exhausted")
        candidates = [p for p in self._profiles if p.supports(required)]
        if not candidates:
            raise ValueError("no profile satisfies requested capabilities")
        if self._recent_evidence is not None:
            eligible = []
            refusals = []
            evidence_reasons = {}
            for profile in candidates:
                if self._identity_for is not None:
                    identity = self._identity_for(profile)
                    if identity is not None and not isinstance(identity, BackendIdentity):
                        raise ValueError("host identity resolver must return a backend identity or None")
                    options = {} if self._evidence_clock is None else {"now": self._evidence_clock()}
                    verdict = self._recent_evidence.assess(
                        profile.model,
                        backend_requirements(required, measured=True),
                        backend=profile.backend, identity=identity,
                        any_of=((frozenset({
                            BackendCapability.TOOL_NATIVE, BackendCapability.TOOL_FALLBACK,
                        }),) if Capability.TOOLS in required else ()),
                        **options,
                    )
                    allowed, reason_code = verdict.state is EvidenceState.PASSED, verdict.reason_code
                elif self._evidence_clock is None:
                    allowed, reason_code = self._recent_evidence.check(
                        profile.model, required, backend=getattr(profile, "backend", "local")
                    )
                else:
                    allowed, reason_code = self._recent_evidence.check(
                        profile.model, required, backend=getattr(profile, "backend", "local"),
                        now=self._evidence_clock()
                    )
                if allowed:
                    eligible.append(profile)
                    evidence_reasons[profile] = reason_code
                else:
                    refusals.append(reason_code)
            if not eligible:
                reason_code = refusals[0] if refusals else "recent_capability_evidence_missing"
                raise CapabilityRoutingError(
                    reason_code,
                    f"no profile has recent passing backend capability evidence ({reason_code})",
                )
            candidates = eligible
        else:
            evidence_reasons = {}
        candidates.sort(key=lambda p: (p.escalation_rank, -p.quality, p.latency_ms, p.model))
        index = (0 if self._identity_for is not None
                 else min(request.escalation_count, len(candidates) - 1))
        trigger = self._trigger(request)
        if self._identity_for is not None:
            trigger = "verifier_failure" if request.verifier_passed is False else None
            if trigger:
                if not request.previous_model:
                    raise CapabilityRoutingError("prior_route_missing", "verified escalation requires the prior model")
                if request.escalation_count >= self._max_escalations:
                    raise CapabilityRoutingError("escalation_limit", "verified escalation budget exhausted")
                previous = next((index for index, profile in enumerate(candidates)
                                 if profile.model == request.previous_model), None)
                if previous is None or previous + 1 >= len(candidates):
                    raise CapabilityRoutingError("escalation_unavailable", "no distinct eligible next model")
                index = previous + 1
            elif request.previous_model is not None:
                previous = next((index for index, profile in enumerate(candidates)
                                 if profile.model == request.previous_model), None)
                if previous is None:
                    raise CapabilityRoutingError("prior_route_unavailable", "prior route is no longer eligible")
                index = previous
        if trigger and request.escalation_count < self._max_escalations and self._identity_for is None:
            index = min(index + 1, len(candidates) - 1)
        profile = candidates[index]
        count = (request.escalation_count + 1 if self._identity_for is not None and trigger
                 else max(request.escalation_count, index))
        escalated = (bool(trigger) if self._identity_for is not None
                     else count > request.escalation_count)
        reason = trigger if escalated else ("initial" if not trigger else "escalation_limit")
        remaining = role_route.budget
        if self._identity_for is not None:
            ceiling = request.requested_budget
            tokens = (None if remaining.output_tokens is None
                      else remaining.output_tokens - request.spent_tokens)
            wall = (None if remaining.wall_seconds is None
                    else remaining.wall_seconds - request.spent_wall_seconds)
            remaining = BudgetLimit(
                steps=min(remaining.steps, ceiling.steps)
                if ceiling is not None and ceiling.steps is not None and remaining.steps is not None
                else remaining.steps,
                output_tokens=min(tokens, ceiling.output_tokens)
                if tokens is not None and ceiling is not None and ceiling.output_tokens is not None
                else tokens,
                wall_seconds=min(wall, ceiling.wall_seconds)
                if wall is not None and ceiling is not None and ceiling.wall_seconds is not None
                else wall,
            )
        return RouteDecision(
            role=request.role, model=profile.model, profile=profile,
            budget=remaining, escalated=escalated, reason=reason,
            escalation_count=count,
            can_escalate=count < self._max_escalations and index < len(candidates) - 1,
            evidence_reason=evidence_reasons.get(profile, ""),
            prior_model=request.previous_model if escalated else None,
            spent_tokens=request.spent_tokens,
            spent_wall_seconds=request.spent_wall_seconds,
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


__all__ = ["CapabilityRouter", "CapabilityRoutingError", "RouteDecision", "RouteOutcome", "RoutingRequest"]
