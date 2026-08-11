"""The developer-workflow tools may not work outside an authorized root.

``fix/cloud-help-drift`` @ ``b8a15ef`` removed ``test_discover``,
``find_references``, ``diff_files`` and ``secret_scan`` from the agent surface
rather than making them dispatchable, and said why in its commit message:

    They are read-only but cannot be made dispatchable as they stand:
    ``harness_tools._resolve_root`` resolves any absolute path with no
    allowed-roots check [...] Adding a dispatch branch would have handed a
    read-only agent unconfined filesystem read. With no dispatch branch they
    were already unreachable, so removal costs no capability.

``sdd/02-calibration`` added the dispatch branches back. The unreachability
that was doing the work went away, and nothing replaced it: a read-only agent
run with ``project=""`` reached ``secret_scan(root=<anything>)`` and got the
credentials printed back to it. Demonstrated against a scratch canary before
the fix, refused after.

So the control cannot be reachability, and it cannot be "we removed the door".
It is confinement in ``_resolve_root``, one layer below all 23 entry points --
which is also the only layer that covers the direct ``@mcp.tool()`` callers,
who were never confined either.

This file is the reason the ``_authorize_pytest_tmp_roots`` fixtures in the
other ``test_harness_*`` files are not just the guard switched off. Those
fixtures authorize the pytest tmp tree; these tests assert that a root outside
the authorized set is still refused, and that the refusal reaches every tool
rather than the one that was audited.
"""
from __future__ import annotations

import pytest

import file_ops
import harness_tools
import server


pytestmark = pytest.mark.unit


# Every harness_tools entry point that resolves a caller-supplied `root`.
# Derived from the module rather than listed, so a tool added later is covered
# on arrival instead of being quietly exempt.
def _root_taking_tools():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(harness_tools))
    names = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        if "_resolve_root" in body:
            names.append(node.name)
    return sorted(names)


ROOT_TAKING_TOOLS = _root_taking_tools()


def test_the_derivation_is_not_vacuous():
    """A walk that silently found nothing would make every test below pass."""
    assert len(ROOT_TAKING_TOOLS) >= 23, ROOT_TAKING_TOOLS
    # `extract_references` is this module's name for the tool the agent
    # surface advertises as `find_references`.
    for expected in ("secret_scan", "diff_files", "extract_references",
                     "test_discover", "git_commit", "build_run"):
        assert expected in ROOT_TAKING_TOOLS, expected


@pytest.mark.parametrize("tool_name", ROOT_TAKING_TOOLS)
def test_an_unauthorized_root_is_refused_by_every_tool(tool_name, tmp_path):
    """Not just the four that were audited: the whole surface, or none of it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PermissionError) as excinfo:
        getattr(harness_tools, tool_name)(root=str(outside))
    assert "outside every authorized root" in str(excinfo.value)


def test_secret_scan_does_not_print_a_secret_from_an_unauthorized_root(tmp_path):
    """The concrete demonstration, kept as a test.

    ``secret_scan`` is the sharp one in the batch because it does not merely
    read the directory, it prints the matches. The value below is AWS's own
    published documentation example key, never a live credential.
    """
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "creds.py").write_text(
        'AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8",
    )
    with pytest.raises(PermissionError):
        harness_tools.secret_scan(root=str(canary))


def test_an_authorized_root_still_works(tmp_path, monkeypatch):
    """The control. A guard that refuses everything is not a guard."""
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    (tmp_path / "creds.py").write_text(
        'AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8",
    )
    result = harness_tools.secret_scan(root=str(tmp_path))
    assert result["ok"] is True
    assert result["findings"], "authorized scan found nothing -- check the fixture"


def test_the_workspace_root_is_authorized_without_configuration():
    """Sonder's own tree is in ``allowed_roots()``, so nothing regresses there."""
    resolved = harness_tools._resolve_root(str(file_ops.workspace_root()))
    assert resolved == file_ops.workspace_root().resolve()


def test_the_host_selected_project_root_is_the_only_scope_channel(tmp_path):
    """``extra_roots`` is a scope, not a tool argument, so a model cannot forge it.

    An ``extra_roots`` *argument* on the agent surface would be a root the model
    grants itself -- the forgery ``_TRUSTED_REPOSITORY_APPROVAL`` exists to stop
    for the guarded file tools. So the authorization travels as an in-process
    scope that only ``server._agent_dispatch`` opens.
    """
    (tmp_path / "t.py").write_text("", encoding="utf-8")

    with pytest.raises(PermissionError):
        harness_tools.test_discover(root=str(tmp_path))

    with harness_tools.authorized_root_scope(str(tmp_path)):
        discovered = harness_tools.test_discover(root=str(tmp_path))
        assert discovered["root"] == str(tmp_path.resolve())

    # And the scope does not leak past its block.
    with pytest.raises(PermissionError):
        harness_tools.test_discover(root=str(tmp_path))


# --- the dispatch surface the regression actually arrived through ----------


READ_ONLY_FOUR = ("test_discover", "find_references", "diff_files", "secret_scan")


@pytest.mark.parametrize("tool_name", READ_ONLY_FOUR)
def test_read_only_dispatch_without_a_project_root_is_refused(tool_name, tmp_path):
    """The reproduction, at the layer it was reproduced on.

    All four grade ``safe``, so the permission gate allows them in every mode --
    it never had a say here. Confinement was the only control and the rootless
    read-only run was the one shape that had none.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    observation = server._agent_dispatch(
        tool_name, {"root": str(outside), "symbol": "x", "left": "a", "right": "b"},
        read_only=True,
    )
    assert observation.startswith("ERROR:"), observation
    assert "AKIA" not in observation
    # Pin WHICH layer answered. Both layers refuse this call, so asserting only
    # "ERROR:" would stay green if either were deleted -- two locks that can
    # only be tested together are one lock.
    assert "no host-selected project root" in observation, observation


@pytest.mark.parametrize("tool_name", READ_ONLY_FOUR)
def test_read_only_dispatch_is_refused_through_the_production_wrapper(
    tool_name, tmp_path,
):
    """`_agent_dispatch_observed` is what autopilot's `observe` policy reaches.

    Asserting only on `_agent_dispatch` would leave the wrapper free to pass a
    project the inner function never sees -- and the wrapper is the reachable
    one (``server.py`` autopilot: ``read_only=(policy == "observe" and not
    unsafe)`` paired with ``project=run.get("project", "")``).
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    observation = server._agent_dispatch_observed(
        tool_name, {"root": str(outside), "symbol": "x", "left": "a", "right": "b"},
        read_only=True, project="",
    )
    assert observation.startswith("ERROR:"), observation
    assert "AKIA" not in observation


def test_a_host_selected_project_root_still_reaches_the_tool(tmp_path):
    """The control for the dispatch layer, so the refusals above mean something."""
    (tmp_path / "t.py").write_text("", encoding="utf-8")
    observation = server._agent_dispatch_observed(
        "test_discover", {"root": str(tmp_path)},
        read_only=True, project=str(tmp_path),
    )
    assert not observation.startswith("ERROR:"), observation
    assert "test discovery" in observation


@pytest.mark.parametrize("tool_name,extra", [
    ("test_discover", {}),
    ("lint_run", {}),
    ("git_branch", {}),
    ("build_run", {"command": "git --version"}),
])
def test_a_write_enabled_run_reaches_the_tool_on_its_bound_project(
    tmp_path, tool_name, extra,
):
    """The control above, on the arm that MUTATES -- which had none at all.

    ``_agent_dispatch_observed`` passed ``repository_extra_roots=project`` on
    the ``read_only`` arm only. The write-enabled arm dispatched with no roots,
    so ``authorized_root_scope("")`` opened empty and every one of the 23
    developer-workflow tools raised ``PermissionError`` on the very project the
    host bound -- the commit that wired these tools in broke them for the exact
    run type that changes anything.

    It shipped green because no test could see it: every write-path test in
    ``test_agent_dispatch_dev_tools.py`` does
    ``monkeypatch.setattr(server, tool_name, ...)``, so ``_resolve_root`` is
    never reached, and the only end-to-end control
    (``test_a_host_selected_project_root_still_reaches_the_tool``) uses
    ``read_only=True``. **Nothing here monkeypatches the tool** -- that is the
    entire point; the real ``_resolve_root`` has to run for this to bind.

    ``tmp_path`` is deliberately outside every configured root, so reaching the
    tool at all is proof the host-selected root was granted. The tools may
    still fail for their own reasons (no git repo, no linter) -- what must not
    appear is the confinement refusal.
    """
    (tmp_path / "t.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    observation = str(server._agent_dispatch_observed(
        tool_name, dict({"root": str(tmp_path)}, **extra),
        read_only=False, project=str(tmp_path),
    ))
    assert "outside every authorized root" not in observation, (
        "%s: a write-enabled run was refused on the project the host bound it "
        "to -- %s" % (tool_name, observation.splitlines()[0])
    )
    assert "PermissionError" not in observation, observation.splitlines()[0]


def test_a_write_enabled_run_is_still_confined_off_its_bound_project(tmp_path):
    """The other half: granting the write arm a root must not grant everything.

    Without this, "fix the write arm" could be satisfied by passing the scope
    unconditionally or by widening ``allowed_roots``. The bound project is
    reachable; a sibling directory outside it is not.
    """
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "t.py").write_text("", encoding="utf-8")
    observation = str(server._agent_dispatch_observed(
        "test_discover", {"root": str(outside)},
        read_only=False, project=str(project),
    ))
    assert observation.startswith("ERROR:"), observation
    assert "outside every authorized root" in observation, observation


# The four tools that take BOTH `root` and a second `path` that harness_tools
# appends straight to the child argv. server._PROJECT_SCOPED_ROOT_AND_PATH_TOOLS
# names the same four and says why.
ROOT_AND_PATH_TOOLS = ("test_run", "lint_run", "format_code", "typecheck_run")


@pytest.mark.parametrize("tool_name", ROOT_AND_PATH_TOOLS)
def test_the_second_path_argument_cannot_escape_the_root(
    tmp_path, monkeypatch, tool_name,
):
    """``_resolve_root`` confines ``root``. Nothing confined ``path``.

    ``lint_run`` (411), ``format_code``, ``test_run`` and ``typecheck_run`` do
    ``cmd.append(path)`` with no check, so the caller-supplied ``path`` reached
    the child argv verbatim and the child ran it relative to ``cwd=root``.
    ``server.py:15659`` -- in the same lane -- already says exactly this:
    *"Containing `root` alone leaves `path` checked by nothing, so
    lint_run(path='../../x', fix=True) would write outside the project."*

    Measured, it is worse than a write. With pytest as the framework this is
    arbitrary **code execution** outside the authorized root::

        test_run(root=<authorized>, path="../OUTSIDE/test_evil.py")
        -> ok=True, "1 passed"
        -> the collected file's module-level/test body ran and wrote a marker
           outside the authorized root

    So ``path`` is load-bearing, not decorative -- which is the fact
    ``_agent_verification_covers`` denies when it justifies reading only
    ``root``. Both halves of that contradiction are fixed together.
    """
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    (outside / "test_evil.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    # Authorize ONLY the project, exactly as a host-selected project scope does.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))

    fn = getattr(harness_tools, tool_name)
    try:
        result = fn(root=str(project), path="../OUTSIDE/test_evil.py")
    except (ValueError, PermissionError) as exc:
        assert "OUTSIDE" in str(exc) or "outside" in str(exc).lower(), exc
        return
    assert result.get("ok") is False, (
        "%s accepted a path escaping its root: %s" % (tool_name, result)
    )
    assert "outside" in str(result.get("error", "")).lower(), result


@pytest.mark.parametrize("tool_name", ROOT_AND_PATH_TOOLS)
def test_a_path_inside_the_root_still_works(tmp_path, monkeypatch, tool_name):
    """The control, so the refusal above is confinement and not a blanket ban.

    Without this, "reject every path" would pass the test above and silently
    remove the narrowing argument these four tools exist to offer.
    """
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    (project / "sub" / "test_ok.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))

    fn = getattr(harness_tools, tool_name)
    result = fn(root=str(project), path="sub/test_ok.py")
    assert "outside" not in str(result.get("error", "")).lower(), result


def test_an_unauthorized_root_is_not_an_existence_oracle(tmp_path, monkeypatch):
    """"Are you allowed?" must be answered before "does it exist?".

    ``_resolve_root`` stat-ed first::

        unauthorized MISSING  -> ValueError: not a directory: <resolved path>
        unauthorized EXISTING -> PermissionError: root is outside every
                                 authorized root: <resolved path>

    Two distinguishable answers, both echoing the resolved host path -- and
    every server wrapper does ``return "ERROR: %s" % exc``, so a confined agent
    got a directory-existence oracle over the whole filesystem plus the real
    location, account name included. Same class as the ``diff_files``
    absolute-path leak this module already fixed.

    Both refusals must now be the same exception with the same text.
    """
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    existing = tmp_path.parent / "oracle_exists"
    existing.mkdir(exist_ok=True)
    missing = tmp_path.parent / "oracle_missing_abc123xyz"

    errors = []
    for target in (existing, missing):
        with pytest.raises(PermissionError) as excinfo:
            harness_tools._resolve_root(str(target))
        errors.append(str(excinfo.value))

    assert errors[0] == errors[1], (
        "an unauthorized existing root and an unauthorized missing one gave "
        "different refusals: %r vs %r" % (errors[0], errors[1])
    )
    for blob, target in zip(errors, (existing, missing)):
        assert str(target) not in blob, "unauthorized path echoed back: %s" % blob
        assert target.name not in blob, "unauthorized path echoed back: %s" % blob


def test_diff_files_does_not_leak_the_absolute_host_path(tmp_path, monkeypatch):
    """``git diff --no-index`` echoes its arguments into the ``diff --git`` header.

    Passing absolute paths therefore printed the operator's full host path --
    where the project lives on disk, and the account name in it -- into every
    diff a *confined* agent read back. Minor next to the confinement bug, and
    fixed with it because the same call site is the cause.
    """
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("two\n", encoding="utf-8")
    result = harness_tools.diff_files(
        root=str(tmp_path), left="a.txt", right="b.txt",
    )
    assert result["ok"] is True
    blob = str(result)
    assert "one" in blob or "two" in blob, "diff produced no content to check"
    assert str(tmp_path) not in blob, "absolute host path leaked into the diff"
    assert "diff --git a/a.txt b/b.txt" in blob


def test_diff_files_refuses_a_path_escaping_its_root(tmp_path, monkeypatch):
    """`left`/`right` were joined to the root and never checked."""
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("two\n", encoding="utf-8")
    result = harness_tools.diff_files(
        root=str(root), left="a.txt", right="../secret.txt",
    )
    assert result["ok"] is False
    assert "inside the root" in result["error"]
