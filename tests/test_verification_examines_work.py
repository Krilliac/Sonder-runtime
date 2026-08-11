"""``build_run`` must name what it examined before it counts as verification.

Task #58. ``_agent_verification_covers`` keys on ``root`` narrowed by ``path``.
``build_run`` has **no ``path`` parameter at all** (``harness_tools.build_run``
takes ``root``/``command``/``timeout``), so the S2/S3 fix -- which made the gate
read ``path`` as well as ``root`` -- cannot reach it. Measured before this fix,
on a project whose only content is one changed file::

    _agent_verification_covers(
        "build_run", {"root": proj, "command": "git --version"}, [<proj/payments.py>],
    ) -> True

and ``harness_tools.build_run(root=proj, command="git --version")`` returns
``ok=True, returncode=0`` on a project with no build system whatsoever. So
``verification_ok`` is granted, ``_work_validated()`` becomes True, and
``autopilot_controller._task_passed`` accepts a whole ``validate`` task on the
strength of a command that examined nothing.

This is the proxy-verification family: "exit 0" accepted as proof of work. The
control copied here is the one ``_agent_validation_covers`` already applies to
``workspace_run`` -- the sibling free-form-argv tool on the *validation* route,
which refuses ``--version``/``--help``/``--dry-run``/inline ``-c``, refuses
clean-only invocations, and otherwise demands either a recognized build or test
action, or explicit path targets covering the changed files. ``build_run``
reached the *verification* route, where no such check existed.

An empty ``command`` is still covered: ``harness_tools.build_run`` then derives
the argv from the root's own build files, a binding the caller cannot forge, and
a root with no build system returns ``ok=False`` so ``tool_ok`` already refuses.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    changed = root / "payments.py"
    changed.write_text("def charge():\n    return 1\n", encoding="utf-8")
    return str(root), [{"path": str(changed), "tool": "file_write"}]


# --- the reproduction ------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git --version",
    "git status",
    "python --version",
    "echo built",
    "cmake --version",
    "npm --help",
])
def test_a_command_that_examined_nothing_does_not_cover_the_work(project, command):
    """The filed defect: exit 0 from an unrelated command satisfied the gate."""
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": command}, mutations,
        project_scope=root,
    ) is False, "%r examines none of the work but was accepted as verification" % command


def test_a_no_op_flag_on_the_real_build_tool_still_covers_nothing(project):
    """``make --version`` is the right program and still builds nothing.

    Matching the project's own build driver is not enough on its own, or the
    check would be satisfied by asking that driver to print its version.
    """
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "make --version"}, mutations,
        project_scope=root,
    ) is False


def test_a_clean_only_command_covers_nothing(project):
    """Deleting the outputs is the opposite of examining the source."""
    root, mutations = project
    for command in ("make clean", "cargo clean", "msbuild /t:clean"):
        assert server._agent_verification_covers(
            "build_run", {"root": root, "command": command}, mutations,
            project_scope=root,
        ) is False, command


def test_an_inline_snippet_covers_nothing(project):
    """``python -c ...`` runs the caller's own text, never the project."""
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "python -c print(1)"}, mutations,
        project_scope=root,
    ) is False


# --- the controls: real builds must keep counting --------------------------

def test_an_auto_detected_build_still_covers_the_work(project):
    """Empty ``command`` lets harness_tools derive argv from the root's own
    build files. That binding is not forgeable by the caller, and a root with
    no build system returns ok=False, so tool_ok refuses it upstream."""
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": ""}, mutations, project_scope=root,
    ) is True


@pytest.mark.parametrize("command", [
    "make",
    "make -j4 all",
    "cargo build",
    "cargo build --release",
    "cmake --build build",
    "go build ./...",
    "npm run build",
    "gradlew build",
    "mvn package -q",
    "msbuild",
    "ninja",
    "dotnet build",
])
def test_a_real_build_command_still_covers_the_work(project, command):
    """Every argv ``harness_tools.build_run`` auto-detects, spelled explicitly,
    plus the drivers ``_agent_validation_covers`` already recognizes. Refusing
    these would break legitimate automation, which is the whole cost of the
    control and is why it is enumerated rather than guessed."""
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": command}, mutations,
        project_scope=root,
    ) is True, "%r is a real build of this root and must still count" % command


def test_a_command_naming_the_changed_file_covers_it(project):
    """An unrecognized program that explicitly targets the changed path is
    judged on the target, exactly as workspace_run's validation route does."""
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "cl /c payments.py"}, mutations,
        project_scope=root,
    ) is True


def test_a_command_naming_a_different_file_does_not_cover_the_change(project):
    root, mutations = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "cl /c other.py"}, mutations,
        project_scope=root,
    ) is False


def test_a_build_outside_the_root_still_falls_closed(project):
    """The pre-existing root check is untouched by the command check."""
    root, mutations = project
    outside = os.path.join(os.path.dirname(root), "elsewhere")
    assert server._agent_verification_covers(
        "build_run", {"root": outside, "command": "make"}, mutations,
        project_scope=root,
    ) is False


# --- the other verifiers must be unaffected --------------------------------

@pytest.mark.parametrize("tool", ["test_run", "lint_run", "typecheck_run"])
def test_path_keyed_verifiers_are_unchanged(project, tool):
    """These have a ``path`` and were fixed by S2; the command check is
    build_run-only and must not reach them."""
    root, mutations = project
    assert server._agent_verification_covers(
        tool, {"root": root, "path": ""}, mutations, project_scope=root,
    ) is True
    assert server._agent_verification_covers(
        tool, {"root": root, "path": "tests"}, mutations, project_scope=root,
    ) is False


def test_the_no_mutation_case_is_still_decided_by_scope(project):
    """A run that changed nothing is answerable for the scope it was confined
    to -- and a command that examines nothing still fails, because it fails
    before the mutation branch is reached."""
    root, _ = project
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "make"}, [], project_scope=root,
    ) is True
    assert server._agent_verification_covers(
        "build_run", {"root": root, "command": "git --version"}, [],
        project_scope=root,
    ) is False
