"""Model-aware application context planning (SPEC-5 WP4 CTX-001/002).

This module is deliberately a small, pure application boundary.  Callers give
it measured/requested token costs; it does not inspect prompts, call a model,
read policy, or perform eviction.  One planner owns the complete section
budget decision, and every section has an independent cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


CONTEXT_SECTIONS = (
    "stable_instructions",
    "policy",
    "goals_plans",
    "tool_schemas",
    "skills",
    "repository_map",
    "working_files",
    "recent_history",
    "memories",
    "subagent_results",
)


@dataclass(frozen=True)
class ModelContext:
    """The model limits needed to make a context decision."""

    model: str
    context_window_tokens: int
    reserved_output_tokens: int

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if not 0 <= self.reserved_output_tokens < self.context_window_tokens:
            raise ValueError(
                "reserved_output_tokens must be non-negative and smaller than "
                "context_window_tokens"
            )

    @property
    def input_budget_tokens(self) -> int:
        return self.context_window_tokens - self.reserved_output_tokens


@dataclass(frozen=True)
class ContextPlan:
    """The immutable result of one context planning pass."""

    model: str
    context_window_tokens: int
    reserved_output_tokens: int
    input_budget_tokens: int
    section_budgets: Mapping[str, int]
    total_section_tokens: int

    def budget_for(self, section: str) -> int:
        try:
            return self.section_budgets[section]
        except KeyError as exc:
            raise KeyError(f"unknown context section {section!r}") from exc


class ContextPlanner:
    """The single deterministic planner for the application context.

    ``requested_tokens`` is the measured amount available for each section and
    ``section_budgets`` is the independently configured ceiling for each one.
    A plan fails closed when the independent ceilings cannot fit the model's
    input budget; choosing what to evict belongs to CTX-003.
    """

    def plan(
        self,
        model: ModelContext,
        requested_tokens: Mapping[str, int],
        section_budgets: Mapping[str, int],
    ) -> ContextPlan:
        self._validate_sections(requested_tokens, "requested_tokens")
        self._validate_sections(section_budgets, "section_budgets")
        selected = {
            section: min(
                self._non_negative(requested_tokens.get(section, 0), section),
                self._budget_for(section_budgets, section),
            )
            for section in CONTEXT_SECTIONS
        }
        total = sum(selected.values())
        if total > model.input_budget_tokens:
            raise ValueError(
                "independent context section budgets exceed the model input "
                f"budget ({total} > {model.input_budget_tokens})"
            )
        return ContextPlan(
            model=model.model,
            context_window_tokens=model.context_window_tokens,
            reserved_output_tokens=model.reserved_output_tokens,
            input_budget_tokens=model.input_budget_tokens,
            section_budgets=MappingProxyType(selected),
            total_section_tokens=total,
        )

    @staticmethod
    def _validate_sections(values: Mapping[str, int], label: str) -> None:
        unknown = set(values).difference(CONTEXT_SECTIONS)
        if unknown:
            raise ValueError(f"{label} contains unknown section(s): {sorted(unknown)}")

    @staticmethod
    def _budget_for(values: Mapping[str, int], section: str) -> int:
        if section not in values:
            raise ValueError(f"missing budget for context section {section!r}")
        return ContextPlanner._non_negative(values[section], section)

    @staticmethod
    def _non_negative(value: int, section: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"token count for context section {section!r} must be non-negative")
        return value
