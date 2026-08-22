"""Focused contract tests for WP5 Workbench/review registry adapters."""

import pytest

from sonder_runtime.application.agent_registry.workbench_review import (
    MAX_CONTEXT_CHARS,
    MAX_PROMPT_CHARS,
    WorkbenchReviewAdapter,
)


class RecordingRegistry:
    def __init__(self):
        self.items = []

    def register(self, registration):
        self.items.append(registration)


def test_registers_workbench_and_read_only_review_modes():
    registry = RecordingRegistry()
    adapter = WorkbenchReviewAdapter()

    installed = adapter.register(registry)

    assert [item.name for item in installed] == ["workbench", "review"]
    assert registry.items[0].mutation_policy == "workspace"
    assert registry.items[0].role == "editor"
    assert registry.items[1].mutation_policy == "read_only"
    assert registry.items[1].role == "reviewer"


def test_invocation_normalizes_mode_and_metadata_without_widening_contract():
    invocation = WorkbenchReviewAdapter().invocation(
        " REVIEW ",
        "Check the proposed change",
        correlation_id="corr-1",
        context="diff summary",
        metadata={"z": "2", "a": "1"},
    )

    assert invocation.registration.name == "review"
    assert invocation.registration.mutation_policy == "read_only"
    assert invocation.metadata == (("a", "1"), ("z", "2"))
    assert invocation.registration.max_steps <= 12


@pytest.mark.parametrize("name", ["", "fleet", "unknown"])
def test_unknown_modes_are_rejected(name):
    with pytest.raises(ValueError):
        WorkbenchReviewAdapter().invocation(name, "inspect", correlation_id="corr-1")


def test_prompt_context_and_correlation_are_bounded():
    adapter = WorkbenchReviewAdapter()
    with pytest.raises(ValueError, match="prompt exceeds"):
        adapter.invocation("workbench", "x" * (MAX_PROMPT_CHARS + 1), correlation_id="c")
    with pytest.raises(ValueError, match="context exceeds"):
        adapter.invocation("review", "inspect", correlation_id="c", context="x" * (MAX_CONTEXT_CHARS + 1))
    with pytest.raises(ValueError, match="correlation_id"):
        adapter.invocation("review", "inspect", correlation_id=" ")
