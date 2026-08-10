"""AGENT_TOOL_HELP and _agent_dispatch must never drift apart.

Before this fix, 23 developer-workflow tools (test_run, test_discover,
build_run, build_clean, lint_run, format_code, typecheck_run, secret_scan,
diff_files, find_references, rename_symbol, apply_patch, the whole
``dependency_*`` family, and every ``git_*`` tool) were advertised in
``AGENT_TOOL_HELP`` -- the agent was told it could call them -- but
``_agent_dispatch`` had no branch for any of them, so every one of those
calls failed with "ERROR: unknown tool '<name>'.". Measured on the unfixed
dispatcher::

    advertised=130 dispatchable=117 missing=23

These tools already exist (``harness_tools.py``), are already registered as
direct ``@mcp.tool()`` functions, and were already anticipated by every other
policy set (``_PROJECT_SCOPED_PATH_TOOLS``, ``_WORK_MUTATION_TOOLS``,
``_WORK_INSPECTION_TOOLS``, ``_AUTOPILOT_WORKSPACE_TOOLS``,
``REPOSITORY_READ_ONLY_TOOLS``) -- only the dispatch branches were missing.

``tool_capabilities.dispatch_names`` AST-parses ``_agent_dispatch``'s own
``tool_name == "x"`` / ``tool_name in (...)`` branches, so it is the
authoritative reader of what the dispatcher can actually call; the drift test
below uses it instead of hand-maintaining a duplicate tool list.
"""
from __future__ import annotations

import pytest

import server
import tool_capabilities as capabilities


DEV_WORKFLOW_TOOLS = frozenset({
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "build_run", "build_clean",
    "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
})

DEV_WORKFLOW_MUTATING_TOOLS = frozenset({
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "dependency_add", "dependency_remove", "dependency_update",
    "build_clean", "rename_symbol", "apply_patch",
})

DEV_WORKFLOW_READ_ONLY_TOOLS = frozenset({
    "test_discover", "find_references", "diff_files", "secret_scan",
})


def _help_advertised_names(help_text):
    names = set()
    for line in help_text.splitlines():
        if line.startswith("- "):
            names.add(line[2:].split(":", 1)[0])
    return names


# --- the real deliverable: the drift invariant ----------------------------


def test_every_advertised_agent_tool_is_dispatchable():
    """The regression test. Must fail on the unfixed dispatcher (see module
    docstring for the exact before-numbers this was run against)."""
    advertised = _help_advertised_names(server.AGENT_TOOL_HELP)
    dispatchable = capabilities.dispatch_names(server._agent_dispatch)
    missing = advertised - dispatchable
    assert missing == set(), (
        "advertised in AGENT_TOOL_HELP but not dispatchable: %s" % sorted(missing)
    )


def test_dev_workflow_tools_are_exactly_what_was_missing():
    # Pins the specific 23-tool batch this task closed, so a future partial
    # revert (e.g. one branch accidentally deleted) is caught by name, not
    # just by the aggregate count above.
    dispatchable = capabilities.dispatch_names(server._agent_dispatch)
    assert DEV_WORKFLOW_TOOLS <= dispatchable


# --- every branch actually reaches its function, with no crash on defaults -


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_TOOLS))
def test_dev_workflow_tool_dispatch_reaches_the_real_function(monkeypatch, tool_name):
    calls = []
    monkeypatch.setattr(
        server, tool_name, lambda *a, **k: calls.append((a, k)) or "ok",
    )
    out = server._agent_dispatch(tool_name, {})
    assert out == "ok"
    assert len(calls) == 1


def test_read_only_dispatch_reaches_test_discover(monkeypatch):
    monkeypatch.setattr(
        server, "test_discover",
        lambda **kwargs: "discovered:" + kwargs.get("root", ""),
    )
    out = server._agent_dispatch("test_discover", {"root": "."}, read_only=True)
    assert out == "discovered:."


def test_git_commit_dispatch_reaches_the_real_function(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "git_commit", lambda **kwargs: calls.append(kwargs) or "committed",
    )
    out = server._agent_dispatch(
        "git_commit", {"root": ".", "message": "fix: x", "paths_json": '["a.py"]'},
    )
    assert out == "committed"
    assert calls[0]["message"] == "fix: x"
    assert calls[0]["paths_json"] == '["a.py"]'


# --- reachable must not mean ungated ---------------------------------------


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_MUTATING_TOOLS))
def test_mutating_dev_workflow_tools_are_in_the_mutation_set(tool_name):
    assert tool_name in server._WORK_MUTATION_TOOLS


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_READ_ONLY_TOOLS))
def test_read_only_dev_workflow_tools_are_not_in_the_mutation_set(tool_name):
    assert tool_name not in server._WORK_MUTATION_TOOLS
    assert server._agent_tool_mutates(tool_name, {}) is False


def test_rename_symbol_mutation_gate_keys_on_dry_run():
    # dry_run defaults True (non-mutating); only explicit False mutates.
    assert server._agent_tool_mutates("rename_symbol", {}) is False
    assert server._agent_tool_mutates("rename_symbol", {"dry_run": True}) is False
    assert server._agent_tool_mutates("rename_symbol", {"dry_run": False}) is True


def test_apply_patch_mutation_gate_keys_on_check_only():
    # Unlike rename_symbol, apply_patch's check_only defaults False -- it
    # applies by default, so only explicit check_only=True is non-mutating.
    assert server._agent_tool_mutates("apply_patch", {}) is True
    assert server._agent_tool_mutates("apply_patch", {"check_only": False}) is True
    assert server._agent_tool_mutates("apply_patch", {"check_only": True}) is False


def test_git_and_dependency_and_build_clean_always_mutate():
    for tool_name in (
        "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
        "git_merge", "git_cherry_pick",
        "dependency_add", "dependency_remove", "dependency_update",
        "build_clean",
    ):
        assert server._agent_tool_mutates(tool_name, {}) is True


# --- project-bound scoping must key on "root", not the wrong default -------


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_TOOLS))
def test_project_scope_rebases_root_for_dev_workflow_tools(tmp_path, tool_name):
    # Before the fix, _project_scoped_path_key defaulted every one of these
    # tools to "path" (they all actually take "root"), so a project-bound
    # agent run silently left the real root argument un-rebased -- it ran
    # against Sonder's own cwd instead of the requested project, while adding
    # a spurious, unused "path" key.
    project = str(tmp_path)
    scoped = server._project_scope_args(tool_name, {"root": "sub"}, project)
    assert scoped["root"] == server.os.path.join(project, "sub")
    assert "path" not in scoped


def test_diff_files_project_scope_rejects_escaping_right_path(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    args = {"root": ".", "left": "a.txt", "right": "../outside.txt"}

    error = server._repository_scope_path_error("diff_files", args, str(project))

    assert error.startswith("ERROR:")
    assert "outside the host-selected project root" in error


def test_project_scoped_test_discover_rebases_and_dispatches_read_only(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    out = server._agent_dispatch_observed(
        "test_discover", {}, read_only=True, project=str(project),
    )

    assert not out.startswith("ERROR:")
