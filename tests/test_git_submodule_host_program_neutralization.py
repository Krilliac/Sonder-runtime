"""The git program guard must also cover submodules' own configuration.

``git status``/``git diff`` in a superproject descend into each checked-out
submodule (a child git run inside it) to learn whether it is dirty.  That
child reads the SUBMODULE's ``.git/config``, whose filter/diff drivers the
superproject's driver probe never saw, so a planted ``filter.<x>.clean`` ran
on a stat-dirty submodule file.  Submodule recursion is now neutralized for
every guarded git command.  Real git, real submodule.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import git_history
import git_tools
import harness_tools
import sonder_runtime.adapters.filesystem.file_ops as file_ops


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _marker_program(tmp_path: Path, marker: Path) -> str:
    script = tmp_path / ("echo-%s.py" % marker.stem)
    script.write_text(
        "import sys\n"
        "with open(%r, 'a', encoding='utf-8') as handle:\n"
        "    handle.write('ran\\n')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n" % str(marker),
        encoding="utf-8",
    )
    return '"%s" "%s"' % (
        str(sys.executable).replace("\\", "/"), str(script).replace("\\", "/"),
    )


def _init(repo):
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Sonder Tests")
    _git(repo, "config", "user.email", "sonder-tests@example.invalid")


@pytest.fixture
def hostile_submodule(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    _init(upstream)
    (upstream / ".gitattributes").write_text("*.txt filter=subhostile\n", encoding="utf-8")
    (upstream / "inner.txt").write_text("inner\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "inner")

    superproject = tmp_path / "super"
    _init(superproject)
    (superproject / "top.txt").write_text("top\n", encoding="utf-8")
    _git(superproject, "add", "top.txt")
    _git(superproject, "commit", "-q", "-m", "top")
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(upstream), "sub")
    _git(superproject, "commit", "-q", "-m", "add submodule")

    submodule = superproject / "sub"
    marker = tmp_path / "subclean.txt"
    # Only the SUBMODULE's own config names the program.
    _git(submodule, "config", "filter.subhostile.clean", _marker_program(tmp_path, marker))
    _git(submodule, "config", "filter.subhostile.required", "true")
    inner = submodule / "inner.txt"
    inner.write_text("inner\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(inner, (later, later))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: superproject)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    return superproject, marker


def test_planted_submodule_program_runs_under_plain_git(hostile_submodule):
    """Guard against a vacuous GREEN: superproject status runs the plant."""
    superproject, marker = hostile_submodule
    _git(superproject, "status", "--porcelain")
    assert marker.exists()


def test_repo_status_does_not_run_submodule_programs(hostile_submodule):
    superproject, marker = hostile_submodule
    result = git_tools.repo_status(str(superproject), bypass=True)
    assert result["branch"] == "main"
    assert not marker.exists()


@pytest.mark.parametrize("staged", [False, True])
def test_repo_diff_does_not_run_submodule_programs(hostile_submodule, staged):
    superproject, marker = hostile_submodule
    (superproject / "top.txt").write_text("changed\n", encoding="utf-8")
    result = git_tools.repo_diff(str(superproject), bypass=True, staged=staged)
    assert "diff" in result
    assert not marker.exists()


def test_git_history_does_not_run_submodule_programs(hostile_submodule):
    superproject, marker = hostile_submodule
    git_history.repo_log(superproject)
    git_history.repo_show(superproject, file_path="top.txt")
    assert not marker.exists()


def test_harness_git_does_not_run_submodule_programs(hostile_submodule):
    superproject, marker = hostile_submodule
    result = harness_tools._run_git(superproject, ["status", "--porcelain"])
    assert result["returncode"] == 0, result
    result = harness_tools._run_git(superproject, ["diff", "HEAD"])
    assert result["returncode"] == 0, result
    assert not marker.exists()


@pytest.mark.parametrize("add_arguments", [["add", "-u"], ["add", "--", "."]])
def test_harness_add_does_not_run_submodule_programs(hostile_submodule, add_arguments):
    """``git add`` inspects populated submodules whatever the ignore config
    says, so the submodule's own drivers must be neutralized by name."""
    superproject, marker = hostile_submodule
    (superproject / "top.txt").write_text("changed\n", encoding="utf-8")
    result = harness_tools._run_git(superproject, add_arguments)
    assert result["returncode"] == 0, result
    assert not marker.exists()


def test_planted_submodule_program_runs_under_git_add_with_ignore_config(hostile_submodule):
    """Vacuity guard: recursion settings alone do not stop ``git add``."""
    superproject, marker = hostile_submodule
    _git(superproject, "-c", "diff.ignoreSubmodules=all", "-c", "submodule.recurse=false",
         "add", "-u")
    assert marker.exists()


def test_nested_submodule_listing_is_bounded():
    from sonder_runtime.adapters import git_program_guard

    def endless(relative, arguments):
        if arguments[-3:] == list(git_program_guard.GITLINK_PROBE_ARGUMENTS):
            return {"returncode": 0, "stdout": "160000 %s 0\tnext\x00" % ("0" * 40)}
        return {"returncode": 1, "stdout": ""}

    with pytest.raises(git_program_guard.GitProgramConfigError):
        git_program_guard.submodule_driver_overrides(endless)
