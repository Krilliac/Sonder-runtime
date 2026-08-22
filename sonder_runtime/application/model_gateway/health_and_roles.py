"""Health, role, and model-truth contracts for the unified model gateway.

This is an application-layer coordinator, not a transport implementation.  A
provider must publish an explicit health state before it can receive a routed
request.  Hardware detection is deliberately separate from runtime readiness:
seeing an NPU in the machine inventory does not prove that a provider can use
it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Mapping, Protocol

from ..context import OperationContext
from ..ports.model_gateway import ModelRequest, ModelResponse
from ...domain.common.errors import DependencyUnavailable, InvalidInput
from ...domain.model_sizing import params_from_model_tag


class ProviderState(StrEnum):
    """Operational state published by a provider adapter."""

    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"

    @property
    def routable(self) -> bool:
        return self in (ProviderState.READY, ProviderState.DEGRADED)


class LogicalRole(StrEnum):
    """Stable gateway roles; none of these names selects a transport."""

    DEFAULT = "default"
    EXPLORER = "explorer"
    ARCHITECT = "architect"
    EDITOR = "editor"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"


@dataclass(frozen=True)
class NpuBoundary:
    """Evidence at the NPU boundary, with detection and readiness distinct."""

    detected: bool = False
    runtime_available: bool = False
    provider_bound: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.detected, bool) or not isinstance(self.runtime_available, bool):
            raise ValueError("NPU detection and runtime availability must be boolean")
        if not isinstance(self.provider_bound, bool):
            raise ValueError("NPU provider binding must be boolean")
        if self.runtime_available and not self.detected:
            raise ValueError("NPU runtime cannot be available when no NPU was detected")
        if self.provider_bound and not self.runtime_available:
            raise ValueError("NPU provider cannot be bound without runtime availability")

    @property
    def ready(self) -> bool:
        """True only when hardware, runtime, and provider binding are evidenced."""
        return self.detected and self.runtime_available and self.provider_bound

    @property
    def status(self) -> str:
        if self.ready:
            return "runtime-ready"
        if self.detected and self.runtime_available:
            return "detected-unbound"
        if self.detected:
            return "detected-unavailable"
        return "not-detected"


@dataclass(frozen=True)
class ProviderHealth:
    """Provider state snapshot consumed by routing; it is not inferred."""

    provider_id: str
    state: ProviderState
    checked_at: datetime
    capabilities: frozenset[str] = frozenset()
    npu: NpuBoundary = NpuBoundary()
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id is required")
        if not isinstance(self.state, ProviderState):
            raise ValueError("state must be a ProviderState")
        if self.checked_at.tzinfo is None:
            object.__setattr__(self, "checked_at", self.checked_at.replace(tzinfo=timezone.utc))
        else:
            object.__setattr__(self, "checked_at", self.checked_at.astimezone(timezone.utc))
        object.__setattr__(self, "capabilities", frozenset(str(v) for v in self.capabilities))

    @property
    def routable(self) -> bool:
        return self.state.routable


@dataclass(frozen=True)
class GatewayBudget:
    """Independent per-role ceilings; no role shares a mutable budget object."""

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    max_concurrent: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens", "max_concurrent"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")


_DEFAULT_BUDGETS = {
    LogicalRole.DEFAULT: GatewayBudget(max_output_tokens=1024, timeout_seconds=300),
    LogicalRole.EXPLORER: GatewayBudget(max_output_tokens=2000, timeout_seconds=120),
    LogicalRole.ARCHITECT: GatewayBudget(max_output_tokens=4000, timeout_seconds=300),
    LogicalRole.EDITOR: GatewayBudget(max_output_tokens=6000, timeout_seconds=600),
    LogicalRole.VERIFIER: GatewayBudget(max_output_tokens=3000, timeout_seconds=300),
    LogicalRole.REVIEWER: GatewayBudget(max_output_tokens=3000, timeout_seconds=240),
    LogicalRole.INTEGRATOR: GatewayBudget(max_output_tokens=4000, timeout_seconds=420),
}


class RoleBudgetBook:
    """Immutable-by-snapshot role budget registry."""

    def __init__(self, budgets: Mapping[LogicalRole, GatewayBudget] | None = None) -> None:
        values = dict(_DEFAULT_BUDGETS)
        if budgets:
            for role, budget in budgets.items():
                if not isinstance(role, LogicalRole) or not isinstance(budget, GatewayBudget):
                    raise ValueError("budgets require LogicalRole/GatewayBudget values")
                values[role] = budget
        self._budgets = MappingProxyType(values)

    def for_role(self, role: LogicalRole) -> GatewayBudget:
        try:
            return self._budgets[LogicalRole(role)]
        except (KeyError, ValueError) as exc:
            raise InvalidInput(f"unknown logical role {role!r}") from exc

    def with_budget(self, role: LogicalRole, budget: GatewayBudget) -> "RoleBudgetBook":
        values = dict(self._budgets)
        values[LogicalRole(role)] = budget
        return RoleBudgetBook(values)

    def snapshot(self) -> Mapping[LogicalRole, GatewayBudget]:
        return self._budgets


@dataclass(frozen=True)
class ModelParameters:
    """Model parameter facts; MoE total and active counts are never conflated."""

    total_b: float
    active_b: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in (self.total_b, self.active_b)):
            raise ValueError("model parameter counts must be positive and finite")
        if self.active_b > self.total_b:
            raise ValueError("active parameters cannot exceed total parameters")

    @property
    def is_moe(self) -> bool:
        return self.active_b < self.total_b

    @classmethod
    def from_tag(cls, tag: str) -> "ModelParameters":
        values = params_from_model_tag(tag)
        if values is None:
            raise InvalidInput(f"model tag has no valid parameter counts: {tag!r}")
        return cls(*values)


@dataclass(frozen=True)
class RoleBinding:
    role: LogicalRole
    provider_id: str
    model: str
    budget: GatewayBudget | None = None
    requires_npu: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.role, LogicalRole) or not self.provider_id.strip() or not self.model.strip():
            raise ValueError("role, provider_id, and model are required")
        if self.budget is not None and not isinstance(self.budget, GatewayBudget):
            raise ValueError("budget must be a GatewayBudget when supplied")


@dataclass(frozen=True)
class GatewayRoute:
    role: LogicalRole
    provider_id: str
    model: str
    budget: GatewayBudget
    health: ProviderHealth
    parameters: ModelParameters | None = None


class _Provider(Protocol):
    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse: ...


class ModelGatewayContract:
    """One role-aware contract over existing ``ModelGateway`` adapters."""

    def __init__(
        self,
        providers: Mapping[str, _Provider],
        bindings: Mapping[LogicalRole, RoleBinding],
        *,
        health: Mapping[str, ProviderHealth] | None = None,
        budgets: RoleBudgetBook | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._bindings = dict(bindings)
        self._budgets = budgets or RoleBudgetBook()
        self._health = dict(health or {})
        for provider_id in self._providers:
            self._health.setdefault(
                provider_id,
                ProviderHealth(provider_id, ProviderState.UNKNOWN, datetime.now(timezone.utc)),
            )

    @property
    def providers(self) -> tuple[str, ...]:
        """Published provider identities, without exposing mutable state."""
        return tuple(sorted(self._providers))

    def publish_health(self, snapshot: ProviderHealth) -> None:
        if snapshot.provider_id not in self._providers:
            raise InvalidInput(f"unknown provider {snapshot.provider_id!r}")
        self._health[snapshot.provider_id] = snapshot

    def health(self, provider_id: str) -> ProviderHealth:
        try:
            return self._health[provider_id]
        except KeyError as exc:
            raise InvalidInput(f"unknown provider {provider_id!r}") from exc

    def route(self, role: LogicalRole = LogicalRole.DEFAULT) -> GatewayRoute:
        try:
            binding = self._bindings[LogicalRole(role)]
        except (KeyError, ValueError) as exc:
            raise InvalidInput(f"no binding for logical role {role!r}") from exc
        snapshot = self.health(binding.provider_id)
        if not snapshot.routable:
            raise DependencyUnavailable(
                f"provider {binding.provider_id!r} is {snapshot.state.value}"
            )
        if binding.requires_npu and not snapshot.npu.ready:
            raise DependencyUnavailable(
                f"provider {binding.provider_id!r} has no runtime-ready NPU ({snapshot.npu.status})"
            )
        parameters = None
        try:
            parameters = ModelParameters.from_tag(binding.model)
        except InvalidInput:
            # Untagged aliases are valid gateway model identifiers; parameter
            # facts remain absent rather than being fabricated.
            pass
        return GatewayRoute(
            LogicalRole(role), binding.provider_id, binding.model,
            binding.budget or self._budgets.for_role(LogicalRole(role)),
            snapshot, parameters,
        )

    def generate(
        self,
        request: ModelRequest,
        context: OperationContext,
        *,
        role: LogicalRole = LogicalRole.DEFAULT,
    ) -> ModelResponse:
        route = self.route(role)
        provider = self._providers[route.provider_id]
        return provider.generate(request, context)


GatewayContract = ModelGatewayContract


__all__ = [
    "GatewayBudget", "GatewayContract", "GatewayRoute", "LogicalRole",
    "ModelGatewayContract", "ModelParameters", "NpuBoundary", "ProviderHealth",
    "ProviderState", "RoleBinding", "RoleBudgetBook",
]
