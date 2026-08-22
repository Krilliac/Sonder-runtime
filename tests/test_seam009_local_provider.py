"""SEAM-009: the typed child port is backed by one bounded local provider."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.subagents import (
    LocalSubagentProvider, UnsupportedSubagentProvider,
)
from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest, LineageRecord, WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentRequest, SubagentStatus,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.domain.agents.roles import AgentRole


def _provider(tmp_path: Path, runner):
    service = DurableContinuationService(
        SQLiteDurableContinuationRepository(tmp_path / "children.db")
    )
    provider = LocalSubagentProvider(service, runner)
    provider.register_root(
        "root-1",
        SubagentBudget(max_steps=8, max_output_tokens=100, max_wall_seconds=30),
    )
    return provider


def test_local_provider_preserves_lineage_and_evidence(tmp_path):
    provider = _provider(tmp_path, lambda state, save, control: "local result")
    request = SubagentRequest(
        "root-1", "do bounded work",
        SubagentBudget(max_steps=4, max_wall_seconds=10, max_output_tokens=20),
        "child-1", (("role", "explorer"),),
    )
    handle = provider.spawn(request, local_owner_context(correlation_id="seam009"))
    result = handle.result(timeout=2)

    assert result.status is SubagentStatus.SUCCEEDED
    assert result.parent_id == "root-1"
    assert provider.snapshot("child-1").parent_id == "root-1"

    root = tmp_path / "repo"
    workspace = WorkspaceAssignment((str(root),), (str(root),))
    preset = resolve_preset("researcher")
    delegation = DelegationRequest(
        "delegation-1",
        LineageRecord("line-1", "root-1", "root-1", "child-1", 1,
                      preset.name, preset.role, workspace),
        "research locally", preset, workspace,
    )
    evidence = DelegationService(provider).integrate(
        delegation, result, verification=("local provider test passed",),
    ).evidence
    assert evidence.status.value == "succeeded"
    assert evidence.output_digest
    assert evidence.usage_steps == result.usage.steps

    assert provider.close(timeout=1) is True


def test_local_provider_rejects_unknown_parent_before_publication(tmp_path):
    provider = _provider(tmp_path, lambda state, save, control: "never")
    request = SubagentRequest("missing", "work", SubagentBudget(max_steps=2), "child")
    with pytest.raises(InvalidSubagentRequest, match="unknown child_id"):
        provider.spawn(request, local_owner_context(correlation_id="unknown-parent"))


def test_local_provider_enforces_output_and_checkpoint_budgets(tmp_path):
    output_provider = _provider(tmp_path / "output", lambda state, save, control: "12345")
    output_request = SubagentRequest(
        "root-1", "work",
        SubagentBudget(max_steps=2, max_wall_seconds=10, max_output_tokens=1), "large",
    )
    output = output_provider.spawn(
        output_request, local_owner_context(correlation_id="output-budget")
    ).result(timeout=2)
    assert output.status is SubagentStatus.TIMED_OUT
    assert output.error is not None

    def too_many_steps(state, save, control):
        save({"step": 1})
        save({"step": 2})
        return "unreachable"

    step_provider = _provider(tmp_path / "steps", too_many_steps)
    step_request = SubagentRequest(
        "root-1", "work",
        SubagentBudget(max_steps=1, max_wall_seconds=10, max_output_tokens=10), "steps",
    )
    steps = step_provider.spawn(
        step_request, local_owner_context(correlation_id="step-budget")
    ).result(timeout=2)
    assert steps.status is SubagentStatus.TIMED_OUT
    assert steps.error is not None


def test_local_provider_cancellation_is_cooperative_and_idempotent(tmp_path):
    def wait_for_cancel(state, save, control):
        while not control.cancelled:
            time.sleep(0.005)
        return "must not succeed"

    provider = _provider(tmp_path, wait_for_cancel)
    handle = provider.spawn(
        SubagentRequest(
            "root-1", "wait",
            SubagentBudget(max_steps=2, max_wall_seconds=10, max_output_tokens=10),
            "cancel-me",
        ),
        local_owner_context(correlation_id="cancel"),
    )
    assert handle.cancel(reason="operator stop") is True
    assert handle.cancel(reason="later reason") is False
    result = handle.result(timeout=2)
    assert result.status is SubagentStatus.CANCELLED
    assert result.error is not None
    assert result.error.message == "operator stop"
    assert provider.close(timeout=1) is True


def test_local_provider_fails_closed_for_unsupported_provider(tmp_path):
    service = DurableContinuationService(
        SQLiteDurableContinuationRepository(tmp_path / "children.db")
    )
    with pytest.raises(UnsupportedSubagentProvider, match="unsupported"):
        LocalSubagentProvider(service, lambda state, save, control: "x", provider="cloud")


def test_local_provider_preserves_nested_lineage(tmp_path):
    provider = _provider(tmp_path, lambda state, save, control: "done")
    first = provider.spawn(
        SubagentRequest(
            "root-1", "first",
            SubagentBudget(max_steps=4, max_wall_seconds=10, max_output_tokens=20), "first",
        ),
        local_owner_context(correlation_id="nested-1"),
    )
    assert first.result(timeout=2).status is SubagentStatus.SUCCEEDED
    second = provider.spawn(
        SubagentRequest(
            "first", "second",
            SubagentBudget(max_steps=2, max_wall_seconds=5, max_output_tokens=10), "second",
        ),
        local_owner_context(correlation_id="nested-2"),
    )
    assert second.result(timeout=2).status is SubagentStatus.SUCCEEDED
    assert provider.snapshot("second").parent_id == "first"
    assert provider.close(timeout=1) is True
