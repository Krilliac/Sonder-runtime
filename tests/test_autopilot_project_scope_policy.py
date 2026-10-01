"""A1: trial3 refused 6-8 calls because policy saw host-injected roots."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import server


BYPASS_REFUSAL = (
    "ERROR: HOST POLICY: autonomous runs cannot use bypass credentials or extra roots."
)


@pytest.fixture
def project_agent(monkeypatch, tmp_path):
    """Fake only model/runner boundaries; keep policy, confinement and IO real."""
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "util.py").write_text("VALUE = 7\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: (
        "fake-model", False, "", "code",
    ))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda tier: None)
    monkeypatch.setattr(server, "_build_system", lambda *a, **k: "test system")
    monkeypatch.setattr(server, "_local_agent_brief", lambda *a: "test host")
    run = {"project": str(project), "policy": "workspace", "tier": "code"}

    def invoke(decisions, **overrides):
        replies = iter(decisions)

        def generate(prompt, history=None):
            return json.dumps(next(replies))

        monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: generate)
        options = dict(
            tier="code", project=str(project), allow_web=False,
            max_steps=len(decisions), include_evidence=True,
            return_host_receipt=True,
            tool_allowlist=server._autopilot_allowed_tools(run),
            tool_policy=server._autopilot_tool_policy(run),
        )
        options.update(overrides)
        return server._agent_impl("Inspect the project", **options)

    return project, run, invoke


@pytest.mark.usefixtures("unattended_effects_allowed")
@pytest.mark.parametrize("tool,args,expected", [
    ("file_read", {"path": "pkg/util.py"}, "VALUE = 7"),
    ("workspace_run", {"program": "python", "args_json": '["-m", "pytest"]'}, "1 passed"),
    ("text_search", {"query": "VALUE", "root": "."}, "VALUE = 7"),
    ("directory_tree", {"path": "."}, "util.py"),
    ("file_edit", {"path": "pkg/util.py", "old": "7", "new": "8"}, ""),
])
def test_agent_checks_model_args_then_dispatches_host_scoped_args(
    monkeypatch, project_agent, tool, args, expected,
):
    """Checking scoped args instead makes every case fail with HOST POLICY."""
    project, run, invoke = project_agent
    policy = server._autopilot_tool_policy(run)
    seen = []

    def check(name, supplied):
        seen.append((name, dict(supplied)))
        return policy(name, supplied)

    def runner(program, args_json="[]", cwd=".", **kwargs):
        assert program == "python"
        assert json.loads(args_json) == ["-m", "pytest"]
        assert Path(cwd).resolve() == project.resolve()
        assert kwargs["extra_roots"] == str(project.resolve())
        return "exit code: 0\n1 passed"

    monkeypatch.setattr(server, "workspace_run", runner)
    result = invoke([
        {"tool": tool, "args": args}, {"final": "Inspection complete."},
    ], tool_policy=check)

    assert BYPASS_REFUSAL not in result.output
    assert seen == [(tool, args)]
    assert tool in result.tools, result.output
    assert expected in result.output
    if tool == "file_edit":
        assert (project / "pkg" / "util.py").read_text(encoding="utf-8") == "VALUE = 8\n"
    if tool == "workspace_run":
        assert result.validation_attempted
        assert result.validation_passed


def test_direct_policy_documents_the_scoped_argument_trap(project_agent):
    project, run, _invoke = project_agent
    policy = server._autopilot_tool_policy(run)
    assert policy("file_read", {"path": "x"}) == ""
    assert policy("file_read", server._project_scope_args(
        "file_read", {"path": "x"}, str(project),
    )) == BYPASS_REFUSAL


@pytest.mark.parametrize("name", ["extra_roots", "approval", "token"])
@pytest.mark.parametrize("value", ["C:/", "trusted", "", None, False, 0, [], {}])
def test_direct_policy_refuses_any_model_supplied_bypass_key(project_agent, name, value):
    _project, run, _invoke = project_agent
    assert server._autopilot_tool_policy(run)(
        "file_read", {"path": "x", name: value},
    ) == BYPASS_REFUSAL


@pytest.mark.parametrize("tool", ["file_read", "text_patch"])
@pytest.mark.parametrize("name,value", [
    ("extra_roots", "C:/"), ("approval", "trusted"), ("token", "model-token"),
    ("extra_roots", ""), ("approval", False), ("token", None),
])
def test_agent_cannot_launder_model_bypass_args_through_host_scoping(
    monkeypatch, project_agent, tool, name, value,
):
    _project, _run, invoke = project_agent
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *a, **k: pytest.fail(
        "a model-supplied bypass argument reached dispatch",
    ))
    result = invoke([
        {"tool": tool, "args": {"path": "pkg/util.py", name: value}},
        {"final": "Stopped."},
    ])
    assert BYPASS_REFUSAL in result.output
    assert not result.tools


def test_direct_policy_keeps_host_text_patch_sentinel(project_agent):
    project, run, _invoke = project_agent
    scoped = server._project_scope_args("text_patch", {}, str(project))
    assert scoped["approval"] is server._TRUSTED_REPOSITORY_APPROVAL
    assert server._autopilot_tool_policy(run)("text_patch", scoped) == ""


@pytest.mark.usefixtures("unattended_effects_allowed")
def test_claim_review_checks_model_args_and_reads_host_project(monkeypatch, project_agent):
    project, run, invoke = project_agent
    reviews = iter([
        {"decision": "continue", "reason": "search the project", "tool": "text_search",
         "args": {"query": "VALUE", "root": "."}},
        {"decision": "accept", "reason": "evidence collected"},
    ])
    monkeypatch.setattr(server, "_agent_negative_claim_review", lambda *a, **k: next(reviews))
    result = invoke([{"final": "There are no matching files."}, {"final": "Review complete."}])
    assert BYPASS_REFUSAL not in result.output
    assert "VALUE = 7" in result.output, result.output
    assert "text_search" in result.tools
    assert result.project_scope == str(project.resolve())


@pytest.mark.parametrize("tool,args", [
    ("file_read", {"path": "../outside.txt"}),
    ("workspace_run", {"program": "python", "args_json": '["-m", "pytest"]', "cwd": ".."}),
    ("workspace_run", {"program": "python", "args_json": '["../outside.py"]'}),
])
def test_project_confinement_still_checks_scoped_arguments(
    monkeypatch, project_agent, tool, args,
):
    _project, _run, invoke = project_agent
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *a, **k: pytest.fail(
        "an out-of-project call reached dispatch",
    ))
    result = invoke([{"tool": tool, "args": args}, {"final": "Stopped."}])
    assert "ERROR:" in result.output
    assert not result.tools


@pytest.mark.parametrize("refusals,stops", [
    (["same", "same", "same"], True),
    (["same", "same"], False),
    (["first", "second", "third"], False),
])
def test_agent_exits_after_three_identical_policy_refusals_across_tools(
    monkeypatch, project_agent, refusals, stops,
):
    _project, _run, invoke = project_agent
    messages = iter(refusals)
    calls = []

    def policy(tool, args):
        calls.append(tool)
        return "ERROR: HOST POLICY: " + next(messages) if len(calls) <= len(refusals) else ""

    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *a, **k: "recovered")
    tools = ["file_read", "text_search", "directory_tree"]
    decisions = [{"tool": tool, "args": {}} for tool in tools[:len(refusals)]]
    decisions += [{"tool": "file_read", "args": {"path": "pkg/util.py"}}, {"final": "Recovered."}]
    result = invoke(decisions, tool_policy=policy)
    if stops:
        assert result.output.startswith("ERROR: host policy refused 3 consecutive calls: same")
        assert calls == tools
        assert not result.tools
    else:
        assert "file_read" in result.tools
        assert len(calls) == len(refusals) + 1
