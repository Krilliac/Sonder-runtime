"""Deterministic built-in agent presets (WP5)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit, RoleBudget, role_budget

logger = logging.getLogger(__name__)


PresetName = Literal["general", "code", "plan", "reviewer", "researcher", "build-test", "integrator"]
AgentCapability = Literal[
    "inspect", "summarize", "research", "edit", "test", "plan", "review",
    "build", "report", "integrate", "validate",
]


@dataclass(frozen=True)
class AgentPreset:
    name: PresetName
    role: AgentRole
    budget: RoleBudget
    capabilities: tuple[AgentCapability, ...]

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.capabilities:
            raise ValueError("preset requires a name and capabilities")


def builtin_presets() -> tuple[AgentPreset, ...]:
    """Return the stable built-ins used by registry adapters."""
    rows = (
        ("general", AgentRole.EXPLORER, ("inspect", "summarize")),
        ("code", AgentRole.EDITOR, ("inspect", "edit", "test")),
        ("plan", AgentRole.ARCHITECT, ("inspect", "plan")),
        ("reviewer", AgentRole.REVIEWER, ("inspect", "review")),
        ("researcher", AgentRole.EXPLORER, ("inspect", "research", "summarize")),
        ("build-test", AgentRole.VERIFIER, ("build", "test", "report")),
    )
    return tuple(AgentPreset(name, role, role_budget(role), caps) for name, role, caps in rows)  # type: ignore[arg-type]


def resolve_preset(name: str, *, max_budget: BudgetLimit | None = None) -> AgentPreset:
    """Resolve a built-in preset and optionally clamp it to a parent ceiling."""
    logger.debug(f"resolve_preset: name={name!r}, max_budget={'set' if max_budget else 'none'}")
    normalized = name.strip().lower()
    preset = next((item for item in builtin_presets() if item.name == normalized), None)
    if preset is None:
        raise KeyError(f"unknown agent preset: {name}")
    if max_budget is None:
        return preset
    current = preset.budget.limit
    for field in ("steps", "output_tokens", "wall_seconds"):
        ceiling = getattr(max_budget, field)
        value = getattr(current, field)
        if ceiling is not None and value is not None:
            if value > ceiling:
                raise ValueError(f"preset {normalized} exceeds parent {field} budget")
            if value > ceiling * 0.8:
                logger.warning(f"preset {normalized!r} {field} approaching parent budget ceiling: {value}/{ceiling}")
    return preset


__all__ = ["AgentCapability", "AgentPreset", "PresetName", "builtin_presets", "resolve_preset"]
