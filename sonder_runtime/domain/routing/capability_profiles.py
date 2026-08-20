"""Provider-neutral capability profiles for model and role routing.

The domain describes what a route can do and how much work it may consume;
it does not select a provider or perform inference.  Scores are measured
inputs, not claims made by the model at request time.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit, role_budget


class Capability(str, Enum):
    PLAN = "plan"
    EDIT = "edit"
    TOOLS = "tools"
    STRUCTURED = "structured"
    VERIFY = "verify"
    SUMMARIZE = "summarize"
    EMBED = "embed"
    VISION = "vision"


@dataclass(frozen=True)
class CapabilityProfile:
    """Measured capability and bounded route metadata for one model."""

    model: str
    capabilities: frozenset[Capability]
    quality: float = 0.5
    latency_ms: int = 0
    context_tokens: int = 1
    escalation_rank: int = 0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must be non-empty")
        if not 0.0 <= self.quality <= 1.0:
            raise ValueError("quality must be between 0 and 1")
        if self.latency_ms < 0 or self.context_tokens <= 0 or self.escalation_rank < 0:
            raise ValueError("profile bounds are invalid")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    def supports(self, required: frozenset[Capability]) -> bool:
        return required.issubset(self.capabilities)


@dataclass(frozen=True)
class RoleRoute:
    """Role-specific capability requirements and independent budget."""

    role: AgentRole
    required: frozenset[Capability]
    budget: BudgetLimit

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")
        object.__setattr__(self, "required", frozenset(self.required))


def default_role_routes() -> dict[AgentRole, RoleRoute]:
    """Return independent, immutable-by-convention role route definitions."""
    required = {
        AgentRole.ARCHITECT: (Capability.PLAN,),
        AgentRole.EDITOR: (Capability.EDIT,),
        AgentRole.VERIFIER: (Capability.VERIFY, Capability.STRUCTURED),
        AgentRole.REVIEWER: (Capability.VERIFY,),
        AgentRole.EXPLORER: (Capability.SUMMARIZE,),
        AgentRole.INTEGRATOR: (Capability.STRUCTURED, Capability.EDIT),
    }
    return {
        role: RoleRoute(role, frozenset(caps), role_budget(role).limit)
        for role, caps in required.items()
    }


__all__ = ["Capability", "CapabilityProfile", "RoleRoute", "default_role_routes"]
