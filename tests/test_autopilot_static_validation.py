"""Real artifact writes through a fake model must have honest host receipts."""
import json
from pathlib import Path

import pytest

import permission_modes
import server


@pytest.fixture
def edits_only(monkeypatch):
    previous = permission_modes.current_mode()
    permission_modes.set_mode("acceptEdits")
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda tool: None)
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    # Disabled speculation still learns transitions. Do not train the shared
    # predictor (or its saved state) with this artificial HTML workflow.
    monkeypatch.setattr(server.sonder_speculation, "default_predictor", lambda: server.sonder_speculation.BranchPredictor())
    monkeypatch.setattr(server.sonder_speculation.BranchPredictor, "save", lambda self: None)
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: ("fake-local", False, False, "code"))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda *a, **k: None)
    monkeypatch.setattr(server, "_agent_verification_standing", lambda: (False, ""))
    yield
    permission_modes.set_mode(previous)


def drive(monkeypatch, root, name, content, *, read=True, verify=False, project=None, relative=False, real_tools=False):
    calls = []
    target = name if relative else str(root / name)
    steps = [
        {"tool": "directory_tree", "args": {"path": "." if relative else str(root)}},
        {"tool": "file_write", "args": {"path": target, "content": content}},
    ]
    if read:
        steps.append({"tool": "file_read", "args": {"path": target}})
    if verify:
        steps.append({"tool": "test_run", "args": {"root": str(root)}})
    steps.append({"final": "Artifact written."})
    replies = iter(json.dumps(step) for step in steps)
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: lambda *a, **k: next(replies))

    def dispatch(tool, args, **kwargs):
        calls.append(tool)
        if tool == "file_write":
            Path(args["path"]).write_text(args["content"], encoding="utf-8")
            return "wrote file"
        if tool == "file_read":
            return Path(args["path"]).read_text(encoding="utf-8")
        if tool == "test_run":
            return "test run (pytest)\n  ok: True\n  returncode: 0"
        return "workspace inspected"

    if not real_tools:
        monkeypatch.setattr(server, "_agent_dispatch_observed", dispatch)
    result = server._agent_impl(
        "Make a self-contained page or script", project=str(root) if project is None else project, max_steps=len(steps),
        auto_checklist=True, return_host_receipt=True,
    )
    return result, calls


def test_html_readback_and_static_check_pass_without_execution(monkeypatch, tmp_path, edits_only):
    result, calls = drive(monkeypatch, tmp_path, "starfield.html", "<!doctype html><html><body><canvas></canvas><script>requestAnimationFrame(()=>{});</script></body></html>")
    assert result.validation_passed, result.output
    assert result.validation_attempted
    assert result.validation_evidence
    assert "test_run" not in calls
    assert not result.output.startswith("VALIDATION_FAILED")


@pytest.mark.parametrize("content", ["<html><body><canvas>", '<html><body><script src="https://cdn.example/x.js"></script></body></html>'])
def test_invalid_or_external_html_cannot_pass(monkeypatch, tmp_path, edits_only, content):
    result, _ = drive(monkeypatch, tmp_path, "bad.html", content)
    assert not result.validation_passed
    assert not result.verification_deferred
    assert result.output.startswith("VALIDATION_FAILED")


@pytest.mark.parametrize("content,passed", [("x = 1\n", True), ("def broken(:\n", False)])
def test_python_is_parsed_without_running(monkeypatch, tmp_path, edits_only, content, passed):
    result, calls = drive(monkeypatch, tmp_path, "page.py", content)
    assert result.validation_passed is passed, result.output
    assert "script_run" not in calls


def test_missing_readback_does_not_claim_static_pass(monkeypatch, tmp_path, edits_only):
    result, _ = drive(monkeypatch, tmp_path, "page.html", "<html></html>", read=False)
    assert not result.validation_passed
    assert not result.verification_deferred


def test_execution_only_artifact_is_explicitly_unverified(monkeypatch, tmp_path, edits_only):
    result, _ = drive(monkeypatch, tmp_path, "shader.glsl", "void main() {}")
    assert not result.validation_passed
    assert result.verification_deferred
    assert result.verification_required
    assert "written, not executed" in result.output
    assert not result.output.startswith("VALIDATION_FAILED")


@pytest.mark.parametrize("explicit_rule", [False, True])
def test_execution_allowed_still_requires_real_verifier(monkeypatch, tmp_path, edits_only, explicit_rule):
    if explicit_rule:
        monkeypatch.setattr(permission_modes, "_rule_lookup", lambda tool: {"action": "allow", "pattern": tool} if tool == "test_run" else None)
    else:
        permission_modes.set_mode("auto")
    unverified, _ = drive(monkeypatch, tmp_path, "page.html", "<html></html>")
    assert not unverified.validation_passed
    assert not unverified.verification_deferred
    verified, calls = drive(monkeypatch, tmp_path, "page.html", "<html></html>", verify=True)
    assert verified.validation_passed, verified.output
    assert "test_run" in calls


def test_default_writing_agent_uses_creation_root(monkeypatch, tmp_path, edits_only):
    workspaces = tmp_path / "Sonder" / "workspaces"
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(workspaces))
    result, _ = drive(monkeypatch, tmp_path, "starfield.html", "<html></html>", project="default", relative=True)
    root = Path(result.project_scope)
    assert root.parent == workspaces.resolve()
    assert (root / "starfield.html").read_text(encoding="utf-8") == "<html></html>"
    assert result.validation_passed, result.output
    assert not (tmp_path / "starfield.html").exists()


def test_real_file_tools_accept_and_check_default_creation(monkeypatch, tmp_path, edits_only):
    workspaces = tmp_path / "Sonder" / "workspaces"
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(workspaces))
    result, _ = drive(monkeypatch, tmp_path, "starfield.html", "<html><body><canvas></canvas></body></html>",
                      project="default", relative=True, real_tools=True)
    assert result.mutation_observed, result.output
    assert result.validation_passed, result.output
    assert Path(result.project_scope).parent == workspaces.resolve()
    assert (Path(result.project_scope) / "starfield.html").is_file()
