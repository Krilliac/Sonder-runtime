"""Typed application path for bounded context assembly.

The planner owns model and section budgets; the selection policy owns which
immutable candidates fit each section.  This use case only composes those
two decisions and returns a new snapshot.  It does not render, compact, or
mutate producer-owned context.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sonder_runtime.domain.context.priority import ContextItem, Selection

from .context_planner import CONTEXT_SECTIONS, ContextPlan, ContextPlanner, ModelContext
from .context_priority import select_context


@dataclass(frozen=True)
class ContextAssembly:
    """An immutable, explainable result for one application context request."""

    plan: ContextPlan
    selections: Mapping[str, Selection]

    def __post_init__(self) -> None:
        selections = dict(self.selections)
        if tuple(selections) != CONTEXT_SECTIONS:
            raise ValueError("selections must contain every context section in order")
        object.__setattr__(self, "selections", MappingProxyType(selections))

    @property
    def selected(self) -> tuple[ContextItem, ...]:
        """Return selected items in stable section and producer order."""
        return tuple(
            item
            for section in CONTEXT_SECTIONS
            for item in self.selections[section].selected
        )

    @property
    def selected_tokens(self) -> int:
        return sum(item.cost for item in self.selected)


class ContextAssemblyService:
    """Compose typed planning and selection for a production caller."""

    def __init__(self, planner: ContextPlanner | None = None) -> None:
        self._planner = planner or ContextPlanner()

    def assemble(
        self,
        model: ModelContext,
        items: Mapping[str, tuple[ContextItem, ...] | list[ContextItem]],
        section_budgets: Mapping[str, int],
    ) -> ContextAssembly:
        """Plan and select items without mutating the supplied candidate lists."""
        unknown = set(items).difference(CONTEXT_SECTIONS)
        if unknown:
            raise ValueError(f"items contains unknown section(s): {sorted(unknown)}")
        candidates = {
            section: tuple(items.get(section, ())) for section in CONTEXT_SECTIONS
        }
        requested = {
            section: sum(item.cost for item in candidates[section])
            for section in CONTEXT_SECTIONS
        }
        plan = self._planner.plan(model, requested, section_budgets)
        selections = {
            section: select_context(candidates[section], budget=plan.budget_for(section))
            for section in CONTEXT_SECTIONS
        }
        result = ContextAssembly(plan, selections)
        if result.selected_tokens > plan.input_budget_tokens:
            raise AssertionError("context assembly exceeded the model input budget")
        return result


__all__ = ["ContextAssembly", "ContextAssemblyService"]
