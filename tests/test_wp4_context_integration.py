"""Focused evidence for the WP4 typed context production adapter."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.context_planning import RuntimeContextPlanningAdapter
from sonder_runtime.application.context_integration import ContextAssemblyService
from sonder_runtime.application.context_planner import CONTEXT_SECTIONS, ModelContext
from sonder_runtime.domain.context.priority import ContextItem


def _budgets(value: int = 100):
    return {section: value for section in CONTEXT_SECTIONS}


def _item(item_id: str, section: str, cost: int, priority: int = 1):
    return ContextItem(item_id, section, cost, priority, "test", ordinal=0)


def test_application_path_plans_then_selects_each_section_within_bounds():
    items = {
        "policy": (_item("policy-1", "policy", 8, 10), _item("policy-2", "policy", 8, 1)),
        "working_files": (_item("file-1", "working_files", 6, 5),),
    }
    result = ContextAssemblyService().assemble(
        ModelContext("model", 40, 10), items, {**_budgets(), "policy": 10, "working_files": 10}
    )

    assert result.plan.input_budget_tokens == 30
    assert [item.item_id for item in result.selected] == ["policy-1", "file-1"]
    assert result.selected_tokens == 14
    assert result.selections["policy"].omitted[0].item_id == "policy-2"


def test_runtime_adapter_uses_selected_context_ceiling_and_preserves_input():
    items = {"recent_history": [_item("turn-1", "recent_history", 5)]}
    original = {section: tuple(value) for section, value in items.items()}
    result = RuntimeContextPlanningAdapter().assemble(
        model="runtime-model", context_tokens="32k", reserved_output_tokens=2_000,
        items=items, section_budgets=_budgets(20),
    )

    assert result.plan.context_window_tokens == 32_000
    assert result.plan.input_budget_tokens == 30_000
    assert tuple(items["recent_history"]) == original["recent_history"]


def test_overcommitted_typed_plan_fails_closed_before_selection():
    with pytest.raises(ValueError, match="exceed the model input budget"):
        ContextAssemblyService().assemble(
            ModelContext("small", 20, 10),
            {"policy": (_item("p", "policy", 20),)},
            _budgets(20),
        )
