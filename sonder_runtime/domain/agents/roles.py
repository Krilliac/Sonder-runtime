"""WP5 agent roles and bounded execution budgets.

The domain layer contains no model or transport choices.  Presets can select
these roles, while adapters enforce the resulting ceilings at execution time.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class AgentRole(str, Enum):
    EXPLORER = "explorer"
    ARCHITECT = "architect"
    EDITOR = "editor"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"


@dataclass(frozen=True)
class BudgetLimit:
    """An optional positive ceiling for one resource."""

    steps: int | None = None
    output_tokens: int | None = None
    wall_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.steps is not None and (isinstance(self.steps, bool) or self.steps <= 0):
            raise ValueError("steps must be a positive integer")
        if self.output_tokens is not None and (
            isinstance(self.output_tokens, bool) or self.output_tokens <= 0
        ):
            raise ValueError("output_tokens must be a positive integer")
        if self.wall_seconds is not None and (
            self.wall_seconds <= 0 or not math.isfinite(self.wall_seconds)
        ):
            raise ValueError("wall_seconds must be positive and finite")


@dataclass(frozen=True)
class RoleBudget:
    role: AgentRole
    limit: BudgetLimit

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")


_DEFAULTS = {
    AgentRole.EXPLORER: BudgetLimit(steps=8, output_tokens=2_000, wall_seconds=120),
    AgentRole.ARCHITECT: BudgetLimit(steps=12, output_tokens=4_000, wall_seconds=300),
    AgentRole.EDITOR: BudgetLimit(steps=20, output_tokens=6_000, wall_seconds=600),
    AgentRole.VERIFIER: BudgetLimit(steps=12, output_tokens=3_000, wall_seconds=300),
    AgentRole.REVIEWER: BudgetLimit(steps=10, output_tokens=3_000, wall_seconds=240),
    AgentRole.INTEGRATOR: BudgetLimit(steps=16, output_tokens=4_000, wall_seconds=420),
}


def role_budget(role: AgentRole) -> RoleBudget:
    """Return the immutable default budget for a role."""
    try:
        return RoleBudget(role, _DEFAULTS[role])
    except KeyError as exc:
        raise ValueError(f"unsupported agent role: {role!r}") from exc


__all__ = ["AgentRole", "BudgetLimit", "RoleBudget", "role_budget"]
