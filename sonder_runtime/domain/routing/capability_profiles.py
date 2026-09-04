"""Provider-neutral capability profiles for model and role routing.

The domain describes what a route can do and how much work it may consume;
it does not select a provider or perform inference.  Scores are measured
inputs, not claims made by the model at request time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

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
        result = required.issubset(self.capabilities)
        if not result:
            missing = required - self.capabilities
            logger.warning(f"model {self.model!r} missing required capabilities {missing}, role may need a different model or tier")
            logger.debug(f"CapabilityProfile({self.model!r}).supports: missing capabilities {missing}")
        return result

    def to_dict(self) -> dict[str, object]:
        """Return the canonical provider/profile record for persistence."""
        return {
            "model": self.model,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "quality": self.quality,
            "latency_ms": self.latency_ms,
            "context_tokens": self.context_tokens,
            "escalation_rank": self.escalation_rank,
        }

    def digest(self) -> str:
        """Return a stable identity for this measured profile."""
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityProfile":
        """Rehydrate a profile while validating capability names at the boundary."""
        _model_hint = repr(value.get("model")) if isinstance(value, Mapping) else "?"
        logger.debug(f"CapabilityProfile.from_dict: model={_model_hint}")
        if not isinstance(value, Mapping):
            raise ValueError("capability profile must be an object")
        capabilities = value.get("capabilities")
        if not isinstance(capabilities, (list, tuple, set, frozenset)):
            raise ValueError("capabilities must be an array")
        try:
            parsed = frozenset(Capability(item) for item in capabilities)
        except (TypeError, ValueError) as exc:
            raise ValueError("capabilities contain an unknown value") from exc
        return cls(
            model=value.get("model", ""),
            capabilities=parsed,
            quality=value.get("quality", 0.5),
            latency_ms=value.get("latency_ms", 0),
            context_tokens=value.get("context_tokens", 1),
            escalation_rank=value.get("escalation_rank", 0),
        )


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
    logger.info("initializing default role routing table")
    logger.debug("building default_role_routes")
    required = {
        AgentRole.ARCHITECT: (Capability.PLAN,),
        AgentRole.EDITOR: (Capability.EDIT,),
        AgentRole.VERIFIER: (Capability.VERIFY, Capability.STRUCTURED),
        AgentRole.REVIEWER: (Capability.VERIFY,),
        AgentRole.EXPLORER: (Capability.SUMMARIZE,),
        AgentRole.INTEGRATOR: (Capability.STRUCTURED, Capability.EDIT),
    }
    routes = {
        role: RoleRoute(role, frozenset(caps), role_budget(role).limit)
        for role, caps in required.items()
    }
    logger.info(f"role routing table ready: {len(routes)} roles configured")
    return routes


__all__ = ["Capability", "CapabilityProfile", "RoleRoute", "default_role_routes"]
