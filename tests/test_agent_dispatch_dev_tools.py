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

import inspect

import pytest

import server
import tool_capabilities as capabilities


@pytest.fixture(autouse=True)
def _effects_answered_for_the_agent(unattended_effects_allowed, every_tool_allowed_by_rule):
    """Every dispatch here reaches a file change, a host program, or worse.

    These tests are about the dispatcher reaching the real function with the
    right arguments; whether the mode gate lets an unattended agent do so is
    ``tests/test_permission_gate_dispatch.py``'s subject. ``auto`` answers
    the file-change and host-program classes for an unattended caller, and a
    written allow rule answers the two graded ``dangerous`` (``git_merge``,
    ``git_cherry_pick``), which no mode answers unattended. The read-only
    refusals asserted below are the dispatcher's own and are unaffected.
    """
    yield


DEV_WORKFLOW_TOOLS = frozenset({
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "build_run", "build_clean",
    "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
})

# Every member of this batch that can change persistent workspace state on
# some invocation -- unconditionally (git_*, dependency_*, build_clean) or
# under an argument (rename_symbol dry_run, apply_patch check_only, lint_run
# fix, format_code check_only).
DEV_WORKFLOW_MUTATING_TOOLS = frozenset({
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "dependency_add", "dependency_remove", "dependency_update",
    "build_clean", "rename_symbol", "apply_patch",
    "lint_run", "format_code",
})

# Tools whose project scoping is handled by a dedicated branch in BOTH
# _repository_scope_path_error and _project_scope_args, so the generic
# single-key path below is never consulted for them.
#
# Every name here is a HOLE in the generic-key invariant, so each one has to be
# earned. This list previously carried seven more -- archive_create,
# diff_files, repo_diff, and all four of
# test_run/lint_run/format_code/typecheck_run -- on a premise that was false
# for them: they have a dedicated branch in _repository_scope_path_error only
# and fall through to the generic key in _project_scope_args. Excluding the
# four verifiers meant the invariant skipped precisely the tools whose
# scoped-key bug 84f8bd1 existed to fix -- a test excluding what it was written
# for. test_every_dedicated_branch_exclusion_is_a_real_dedicated_branch now
# reads both scopers from source and fails if this list claims a hole it has
# not earned.
DEDICATED_SCOPE_BRANCH_TOOLS = frozenset({
    "archive_extract", "context_pack", "data_convert",
    "file_batch_write", "file_copy", "file_move",
    "text_patch", "workspace_compare",
})

DEV_WORKFLOW_READ_ONLY_TOOLS = frozenset({
    "test_discover", "find_references", "diff_files", "secret_scan",
})

# The four tools that take BOTH `root` and a `path` that harness_tools appends
# straight to the child argv (harness_tools.py:233, 353, 366, 394).
DEV_WORKFLOW_PATH_ARG_TOOLS = frozenset({
    "test_run", "lint_run", "format_code", "typecheck_run",
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
    # Sentinel: _help_advertised_names returns set() for any help text whose
    # lines stop starting with "- ", and an empty `advertised` makes the
    # difference below empty too -- the invariant would pass while measuring
    # nothing, which is exactly what an infrastructure failure looks like in
    # the one test whose whole job is to notice. 130 are advertised today.
    assert len(advertised) >= 120, (
        "AGENT_TOOL_HELP parsed as only %d advertised tools -- the help format "
        "changed and this drift test is no longer measuring anything"
        % len(advertised)
    )
    dispatchable = capabilities.dispatch_names(server._agent_dispatch)
    missing = advertised - dispatchable
    assert missing == set(), (
        "advertised in AGENT_TOOL_HELP but not dispatchable: %s" % sorted(missing)
    )


def test_drift_invariant_fails_loudly_when_the_help_format_changes(monkeypatch):
    # Proves the sentinel above is load-bearing rather than decorative: feed
    # the drift test a help text its parser cannot read and it must fail, not
    # pass with an empty set on both sides.
    monkeypatch.setattr(
        server, "AGENT_TOOL_HELP", "Available tools:\n* file_read: {}\n",
    )
    with pytest.raises(AssertionError, match="AGENT_TOOL_HELP parsed"):
        test_every_advertised_agent_tool_is_dispatchable()


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


def test_read_only_dispatch_reaches_test_discover(monkeypatch, tmp_path):
    """A read-only dispatch reaches the tool THROUGH a host-selected project.

    This test used to assert that ``_agent_dispatch("test_discover",
    {"root": "."}, read_only=True)`` reached the tool with no project bound at
    all -- the vulnerability written down as a requirement.
    ``_agent_project_scope("")`` returns ``("", "")`` with no error, and every
    path check in the read-only block is conditional on a project root being
    present, so the rootless run was the one shape with no confinement:
    ``secret_scan`` on that path read a canary file outside the repository and
    printed the key back.

    The intent is still worth keeping -- read-only dispatch must actually reach
    the read-only tools -- so it is asserted the way the tool is genuinely used,
    with a root bound, plus the negative half that pins the fix.
    """
    monkeypatch.setattr(
        server, "test_discover",
        lambda **kwargs: "discovered:" + kwargs.get("root", ""),
    )
    out = server._agent_dispatch(
        "test_discover", {"root": "."}, read_only=True,
        repository_extra_roots=str(tmp_path),
    )
    assert out == "discovered:%s" % tmp_path

    refused = server._agent_dispatch(
        "test_discover", {"root": "."}, read_only=True,
    )
    assert refused.startswith("ERROR:")
    assert "no host-selected project root" in refused


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


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_MUTATING_TOOLS))
def test_mutating_dev_workflow_tools_are_refused_by_read_only_dispatch(
    monkeypatch, tool_name, tmp_path,
):
    """Assert the gate at the enforcement layer, not the classification layer.

    All 23 branches were appended at the *end* of ``_agent_dispatch`` and are
    gated purely by sitting below the ``read_only`` policy block. Membership in
    ``_WORK_MUTATION_TOOLS`` cannot see that: a refactor that hoisted or
    reordered the branches would bypass the gate with every membership test
    still green. So assert both halves of the differential -- the same call is
    refused *and never reaches the tool* under ``read_only=True``, and does
    reach it when the gate is off. The second half is what stops this test
    passing vacuously (e.g. on a misspelled tool name, where "ERROR:" would be
    returned for the wrong reason).

    A project root is bound here on purpose. Without one, ``_agent_dispatch``
    now refuses every project-scoped tool outright -- the second layer added
    for the rootless-read-only hole -- and this test would have gone on passing
    while proving only that, never reaching the read-only policy it exists to
    check. Same refusal, different reason, and the difference is the whole
    test. ``tmp_path`` is the host-selected root, so the refusal below comes
    from ``_repository_read_only_error``: these tools are not in
    ``REPOSITORY_READ_ONLY_TOOLS``.
    """
    calls = []
    monkeypatch.setattr(
        server, tool_name, lambda *a, **k: calls.append((a, k)) or "ran",
    )

    refused = server._agent_dispatch(
        tool_name, {"root": "."}, read_only=True,
        repository_extra_roots=str(tmp_path),
    )

    assert refused.startswith("ERROR:")
    assert "no host-selected project root" not in refused, (
        "%s was refused for want of a project root, not by the read-only "
        "policy -- this test is no longer checking what it says" % tool_name
    )
    assert calls == [], "%s executed despite read_only=True" % tool_name

    assert server._agent_dispatch(tool_name, {"root": "."}) == "ran"
    assert len(calls) == 1


def test_lint_run_mutation_gate_keys_on_fix():
    # lint_run(fix=True) runs the linter's *fix* command -- `ruff check
    # --fix`, `npx eslint --fix`, `cargo clippy --fix` -- all of which rewrite
    # source files. fix=True counts as a mutation even for linters that have
    # no fix command (flake8, pylint): the linter is auto-detected inside the
    # tool, long after this gate has had to decide, so the gate must be honest
    # about not knowing which one will run.
    assert server._agent_tool_mutates("lint_run", {}) is False
    assert server._agent_tool_mutates("lint_run", {"fix": False}) is False
    assert server._agent_tool_mutates("lint_run", {"fix": True}) is True


def test_format_code_mutation_gate_keys_on_check_only():
    # Every formatter in the table writes in place by default (`ruff format`,
    # `black`, `prettier --write`, `cargo fmt`, `gofmt -w`, `clang-format
    # -i`), so like apply_patch this one applies unless explicitly told only
    # to check.
    assert server._agent_tool_mutates("format_code", {}) is True
    assert server._agent_tool_mutates("format_code", {"check_only": False}) is True
    assert server._agent_tool_mutates("format_code", {"check_only": True}) is False


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


def test_generic_scoped_path_key_names_a_real_parameter_of_every_tool():
    """The generic containment branch checks exactly one argument key.

    If that key is not a parameter the tool actually accepts, the branch reads
    a missing key, falls back to ``"."``, and ``"."`` always resolves inside
    the project root -- so the check passes unconditionally on every input.
    That is a guard that silently no-ops, which is the defect class this task
    exists to close, so enumerate it from the real signatures rather than
    trusting any hand-maintained list (this one included).
    """
    offenders = []
    for name in sorted(server._PROJECT_SCOPED_PATH_TOOLS):
        if name in DEDICATED_SCOPE_BRANCH_TOOLS:
            continue
        tool = getattr(server, name, None)
        assert tool is not None, "%s is project-scoped but not defined" % name
        parameters = sorted(inspect.signature(tool).parameters)
        key = server._project_scoped_path_key(name)
        if key not in parameters:
            offenders.append(
                "%s: scoped key %r is not one of its parameters %s"
                % (name, key, parameters)
            )
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_PATH_ARG_TOOLS))
def test_both_arg_tools_rebase_root_and_leave_path_alone(tmp_path, tool_name):
    # These four accept `path` AND `root`. A scoping rule that rebased the
    # first path-shaped argument it found would pick `path` and leave
    # root="." -- so the tool would run in Sonder's own working directory
    # while reporting the project's name, succeeding silently against the
    # wrong tree. `root` is the argument that must move. `path` must not: the
    # child interprets it relative to `root` (cwd=root), and for the cargo and
    # go frameworks it is a target/package selector, not a path at all.
    project = str(tmp_path)

    scoped = server._project_scope_args(tool_name, {"path": "tests/unit"}, project)

    assert scoped["root"] == project
    assert scoped["path"] == "tests/unit"


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_PATH_ARG_TOOLS))
def test_root_and_path_tools_reject_an_escaping_relative_path(tmp_path, tool_name):
    # `root` alone being contained is not enough: harness_tools appends `path`
    # to the child argv, so lint_run(path="../outside", fix=True) and
    # format_code(path="../outside") WRITE outside the host-selected project.
    project = tmp_path / "project"
    project.mkdir()
    args = {"root": ".", "path": "../outside", "fix": True}

    error = server._repository_scope_path_error(tool_name, args, str(project))

    assert error.startswith("ERROR:")
    assert "outside the host-selected project root" in error


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_PATH_ARG_TOOLS))
def test_root_and_path_tools_reject_an_escaping_absolute_path(tmp_path, tool_name):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    error = server._repository_scope_path_error(
        tool_name, {"root": ".", "path": str(outside)}, str(project),
    )

    assert error.startswith("ERROR:")
    assert "outside the host-selected project root" in error


@pytest.mark.parametrize("tool_name", sorted(DEV_WORKFLOW_PATH_ARG_TOOLS))
def test_root_and_path_tools_allow_omitted_and_contained_paths(tmp_path, tool_name):
    # The containment check must not over-block: `path` is optional, and it is
    # resolved against the tool's OWN root (which is what the child process
    # does), not against the project root.
    project = tmp_path / "project"
    project.mkdir()

    assert server._repository_scope_path_error(
        tool_name, {"root": "."}, str(project),
    ) == ""
    assert server._repository_scope_path_error(
        tool_name, {"root": ".", "path": "src/pkg"}, str(project),
    ) == ""
    assert server._repository_scope_path_error(
        tool_name, {"root": "sub", "path": "../other"}, str(project),
    ) == ""


# --- build_run forwards argv, so it needs the argv guard -------------------


@pytest.mark.parametrize("command", [
    "powershell -Command Remove-Item x",
    "python -c import os",
    "uv run python -c print(1)",
    "bash -c make",
    "node -e process.exit()",
    "cmd /c del x",
])
def test_build_run_command_rejects_inline_interpreters(tmp_path, command):
    # build_run forwards a caller-supplied `command` straight to a child
    # process (harness_tools.py:657-663). Making it dispatchable therefore
    # opened an execution route around the inline-interpreter guard that was
    # written for exactly this risk on workspace_run/script_run.
    project = tmp_path / "project"
    project.mkdir()

    error = server._agent_project_execution_argument_error(
        "build_run", {"root": str(project), "command": command}, str(project),
    )

    assert error.startswith("ERROR:")
    assert "inline interpreter" in error


def test_build_run_command_rejects_an_argv_path_outside_the_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    error = server._agent_project_execution_argument_error(
        "build_run", {"root": str(project), "command": "make -C ../outside"},
        str(project),
    )

    assert error.startswith("ERROR:")
    assert "outside the host-selected project root" in error


@pytest.mark.parametrize("command", [
    "", "make", "cmake --build build", "npm run build", "cargo build --release",
    "go build ./...",
])
def test_build_run_allows_ordinary_build_commands(tmp_path, command):
    project = tmp_path / "project"
    project.mkdir()

    assert server._agent_project_execution_argument_error(
        "build_run", {"root": str(project), "command": command}, str(project),
    ) == ""


def test_build_run_command_guard_is_project_bound_only():
    # Documented residual: like workspace_run, this guard only engages when the
    # host has bound the agent to a project. An unbound agent's build_run is
    # governed by permission_modes.risk_of("build_run") == "execution", not by
    # this argument guard.
    assert server._agent_project_execution_argument_error(
        "build_run", {"command": "powershell -Command x"}, "",
    ) == ""


def test_project_scoped_test_discover_rebases_and_dispatches_read_only(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    out = server._agent_dispatch_observed(
        "test_discover", {}, read_only=True, project=str(project),
    )

    assert not out.startswith("ERROR:")


def _tool_name_branches(function_name):
    """Tool names that ``function_name`` compares ``tool_name`` against.

    Read from the source rather than from a list someone maintains, because a
    hand-kept exclusion list is exactly the thing that drifted.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(server.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        names = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Compare):
                continue
            if not (isinstance(inner.left, ast.Name) and inner.left.id == "tool_name"):
                continue
            for comparator in inner.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(
                    comparator.value, str
                ):
                    names.add(comparator.value)
                elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                    for element in comparator.elts:
                        if isinstance(element, ast.Constant) and isinstance(
                            element.value, str
                        ):
                            names.add(element.value)
        return names
    raise AssertionError("no function named %r in server.py" % function_name)


def test_every_dedicated_branch_exclusion_is_a_real_dedicated_branch():
    """An exclusion list is a hole in a test; each hole must be earned.

    ``DEDICATED_SCOPE_BRANCH_TOOLS`` skips names in the generic-key invariant
    on the stated premise that each has a dedicated branch in BOTH
    ``_repository_scope_path_error`` and ``_project_scope_args``. Where that
    premise is false the tool is simply unchecked -- and the names it was false
    for were ``test_run``/``lint_run``/``format_code``/``typecheck_run``, the
    exact tools whose scoped-key bug ``84f8bd1`` existed to fix. A test that
    excludes what it was written for.
    """
    repository = _tool_name_branches("_repository_scope_path_error")
    project = _tool_name_branches("_project_scope_args")
    # Sentinels: an ast walk that silently stopped matching would otherwise
    # make every exclusion look unjustified, or justify all of them.
    assert len(repository) >= 10 and len(project) >= 8

    unearned = sorted(DEDICATED_SCOPE_BRANCH_TOOLS - (repository & project))

    assert unearned == [], (
        "these are excluded from the generic-key invariant but have no "
        "dedicated branch in both scopers, so nothing checks them: %s" % unearned
    )
