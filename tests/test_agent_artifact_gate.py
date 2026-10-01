"""Selecting non-executing evidence never grants permission to run a tool."""
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import agent_artifact_gate as gate


@pytest.fixture
def refused(monkeypatch):
    calls = []

    def decide(tool, **kwargs):
        calls.append((tool, kwargs))
        return SimpleNamespace(allowed=False)

    monkeypatch.setattr(gate.permission_policy, "decide_for_caller", decide)
    return calls


def test_preflight_does_not_spend_approval_or_record_execution(refused):
    assert gate.refused_execution_verifiers({"test_run", "file_read"}) == ("test_run",)
    assert refused == [("test_run", {
        "interactive": False, "gate_control_exempt": False,
        "surface": "autopilot-verification-preflight", "record": False,
    })]
    assert gate.refused_execution_verifiers({"file_read"}) == ()


@pytest.mark.parametrize("instruction", ["Inspect file contents", "Run static syntax checks", "Read-back the written artifact"])
def test_static_validation_task_is_not_deferred(refused, instruction):
    assert gate.validation_deferral({"test_run"}, {"instruction": instruction}, "root") is None


def test_execution_task_deferral_names_requested_tool_and_command(refused):
    fields = gate.validation_deferral({"test_run", "workspace_run"}, {
        "instruction": "Use workspace_run to run `python -m pytest -q`",
    }, "root")
    assert fields["verification_deferred"]
    assert "workspace_run" in fields["verification_required"]
    assert "python -m pytest -q" in fields["output"]
    assert "validation_passed" not in fields


def test_any_permitted_verifier_retains_execution_requirement(monkeypatch):
    monkeypatch.setattr(gate.permission_policy, "decide_for_caller",
                        lambda tool, **kwargs: SimpleNamespace(allowed=tool == "run_project"))
    assert gate.refused_execution_verifiers() == ()


@pytest.mark.parametrize("execution,success", [(True, True), (False, False)])
def test_partial_effects_cannot_be_statically_rescued(tmp_path, refused, execution, success):
    artifact = tmp_path / "page.html"
    artifact.write_text("<html></html>", encoding="utf-8")
    evidence = gate.AgentArtifactGate(tmp_path, "self-contained")
    evidence.observe("file_write", {"path": str(artifact)}, "", success=success, mutation=True, execution=execution)
    evidence.observe("file_read", {"path": str(artifact)}, "", success=True)
    assert not evidence.assess()["passed"]
    assert not evidence.assess()["deferred"]
