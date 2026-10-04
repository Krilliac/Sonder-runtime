"""Role based context defaults for verifier/reviewer delegation."""
from __future__ import annotations

from pathlib import Path

from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    LineageRecord,
    WorkspaceAssignment,
    complete_builtin_presets,
)
from sonder_runtime.application.agents.workflow_integration import AgentWorkflowService
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest
from sonder_runtime.application.worker_registry.service import WorkerRegistryService
from sonder_runtime.application.ports.worker_registry import (
    WorkerContextPolicy,
    WorkerExecutionContract,
)
from sonder_runtime.domain.agents.roles import AgentRole


def _request(role: AgentRole, *, contract: WorkerExecutionContract | None = None):
    preset = next(item for item in complete_builtin_presets() if item.role is role)
    root = Path("D:/scoped-review-test")
    workspace = WorkspaceAssignment((str(root),), ())
    lineage = LineageRecord(
        "lineage", "root", "parent", "child", 1, preset.name, role, workspace,
    )
    return DelegationRequest(
        "delegation", lineage, "task/spec: fix the candidate", preset, workspace,
        ("diff: candidate.patch", "test: pytest -q"),
        execution_contract=contract or WorkerExecutionContract(),
    )


def test_reviewer_and_verifier_default_to_scoped_with_three_evidence_inputs():
    for role in (AgentRole.VERIFIER, AgentRole.REVIEWER):
        request = _request(role)
        contract = request.execution_contract
        assert contract.context_policy is WorkerContextPolicy.SCOPED
        assert tuple(item.reference for item in contract.context_inputs) == (
            "diff/artifacts", "task/spec", "test evidence",
        )


def test_explicit_clean_override_is_preserved():
    request = _request(AgentRole.REVIEWER, contract=WorkerExecutionContract(
        context_policy=WorkerContextPolicy.CLEAN,
    ))
    assert request.execution_contract.context_policy is WorkerContextPolicy.CLEAN
    assert request.execution_contract.context_inputs == ()


def test_explicit_contract_fields_are_preserved_when_policy_is_unspecified():
    contract = WorkerExecutionContract(
        success_criteria=("tests pass",),
        context_policy=WorkerContextPolicy.UNSPECIFIED,
    )
    request = _request(AgentRole.REVIEWER, contract=contract)
    assert request.execution_contract.success_criteria == contract.success_criteria
    assert request.execution_contract.context_policy is WorkerContextPolicy.SCOPED
    assert request.context_policy_defaulted is False  # registry gate remains mandatory


def test_all_role_defaults_and_critic_adapter_scope():
    changed = []
    for role in AgentRole:
        request = _request(role)
        if request.execution_contract.context_policy is WorkerContextPolicy.SCOPED:
            changed.append(role.value)
        else:
            assert request.execution_contract == WorkerExecutionContract()
    assert changed == ["verifier", "reviewer"]
    launch = WorkerRegistryService.launch_for(SubagentRequest(
        "parent", "review the candidate", SubagentBudget(max_steps=1),
        child_id="critic-child", metadata=(("worker_role", "critic"),),
    ))
    assert launch.role == "critic"
    assert launch.execution_contract.context_policy is WorkerContextPolicy.SCOPED


def test_registry_role_default_preserves_other_execution_metadata():
    launch = WorkerRegistryService.launch_for(SubagentRequest(
        "parent", "review the candidate", SubagentBudget(max_steps=1),
        child_id="reviewer-child", metadata=(
            ("role", "reviewer"), ("execution_success_criteria", '["tests pass"]'),
            ("execution_verification_commands", '[["pytest", "-q"]]'),
        ),
    ))
    assert launch.execution_contract.success_criteria == ("tests pass",)
    assert launch.execution_contract.verification_commands == (("pytest", "-q"),)
    assert launch.execution_contract.context_policy is WorkerContextPolicy.SCOPED


def test_direct_worker_registry_launch_applies_role_default():
    launch = WorkerRegistryService.launch_for(SubagentRequest(
        "parent", "review the candidate", SubagentBudget(max_steps=1),
        child_id="reviewer-child", metadata=(("role", "reviewer"),),
    ))
    assert launch.execution_contract.context_policy is WorkerContextPolicy.SCOPED


def test_direct_worker_registry_honors_explicit_clean_override():
    launch = WorkerRegistryService.launch_for(SubagentRequest(
        "parent", "review the candidate", SubagentBudget(max_steps=1),
        child_id="reviewer-child", metadata=(
            ("role", "reviewer"), ("execution_context_policy", "clean"),
        ),
    ))
    assert launch.execution_contract.context_policy is WorkerContextPolicy.CLEAN


def test_direct_worker_registry_honors_explicit_inherit_override():
    digest = "a" * 64
    launch = WorkerRegistryService.launch_for(SubagentRequest(
        "parent", "review the candidate", SubagentBudget(max_steps=1),
        child_id="reviewer-child", metadata=(
            ("worker_role", "reviewer"),
            ("execution_context_policy", "inherit"),
            ("execution_inherited_context_sha256", digest),
        ),
    ))
    assert launch.execution_contract.context_policy is WorkerContextPolicy.INHERIT
    assert launch.execution_contract.inherited_context_sha256 == digest


def test_scoped_workflow_prompt_excludes_rationale_field():
    prompt = AgentWorkflowService._next_prompt(
        AgentRole.REVIEWER,
        "implementer rationale: I chose this because it felt simplest\n"
        "```diff\n--- a/app.py\n+++ b/app.py\n@@\n+return 1\n```",
        task_spec="make the result deterministic",
        artifacts=("artifact: report.json",),
        test_evidence=("pytest: 3 passed",),
    )
    assert "Task/spec:" in prompt
    assert "Diff/artifacts:" in prompt
    assert "Test evidence:" in prompt
    assert "implementer rationale" not in prompt.lower()
    assert "return 1" in prompt
