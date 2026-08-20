"""WP4 CTX-001/002: one model-aware planner with independent budgets."""
from __future__ import annotations

import pytest

from sonder_runtime.application.context_planner import (
    CONTEXT_SECTIONS,
    ContextPlanner,
    ModelContext,
)


def budgets(value: int = 100):
    return {section: value for section in CONTEXT_SECTIONS}


def requested(value: int = 10):
    return {section: value for section in CONTEXT_SECTIONS}


class TestContextPlanner:
    def test_one_plan_covers_every_ctx002_section(self):
        plan = ContextPlanner().plan(
            ModelContext("local-model", 1_000, 200), requested(), budgets()
        )
        assert tuple(plan.section_budgets) == CONTEXT_SECTIONS
        assert plan.total_section_tokens == 100
        assert plan.input_budget_tokens == 800

    def test_model_window_and_output_reserve_are_accounted_for(self):
        plan = ContextPlanner().plan(
            ModelContext("small", 150, 50), requested(10), budgets(20)
        )
        assert plan.input_budget_tokens == 100
        assert plan.total_section_tokens == 100

    def test_each_section_is_capped_independently(self):
        caps = budgets(100)
        caps["working_files"] = 7
        counts = requested(10)
        plan = ContextPlanner().plan(ModelContext("m", 500, 20), counts, caps)
        assert plan.budget_for("working_files") == 7
        assert plan.budget_for("memories") == 10

    def test_changing_one_cap_does_not_rebalance_other_sections(self):
        caps = budgets(20)
        first = ContextPlanner().plan(ModelContext("m", 500, 20), requested(10), caps)
        caps["skills"] = 1
        second = ContextPlanner().plan(ModelContext("m", 500, 20), requested(10), caps)
        assert second.budget_for("skills") == 1
        assert second.budget_for("memories") == first.budget_for("memories")

    def test_overcommitted_plan_fails_closed(self):
        with pytest.raises(ValueError, match="exceed the model input budget"):
            ContextPlanner().plan(ModelContext("m", 50, 10), requested(10), budgets(10))

    @pytest.mark.parametrize("bad", [-1, True, "10"])
    def test_negative_or_non_integer_counts_are_rejected(self, bad):
        counts = requested()
        counts["policy"] = bad
        with pytest.raises(ValueError, match="non-negative"):
            ContextPlanner().plan(ModelContext("m", 500, 20), counts, budgets())

    def test_unknown_or_missing_sections_are_rejected(self):
        with pytest.raises(ValueError, match="unknown section"):
            ContextPlanner().plan(
                ModelContext("m", 500, 20), {"not_a_section": 1}, budgets()
            )
        caps = budgets()
        del caps["policy"]
        with pytest.raises(ValueError, match="missing budget"):
            ContextPlanner().plan(ModelContext("m", 500, 20), requested(), caps)

    def test_plan_is_immutable(self):
        plan = ContextPlanner().plan(ModelContext("m", 500, 20), requested(), budgets())
        with pytest.raises(TypeError):
            plan.section_budgets["policy"] = 99  # type: ignore[index]
