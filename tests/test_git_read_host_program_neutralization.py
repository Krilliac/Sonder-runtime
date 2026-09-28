"""Read-side git tools must not run programs named by repository config.

``repo_status``/``repo_diff`` (git_tools) and ``repo_log``/``repo_show``/
``repo_blame`` (git_history) are read-only tools, so they run without any
approval.  A repository's ``core.fsmonitor``, a filter ``clean`` program run
on a stat-dirty file, a diff ``textconv`` or an external diff driver is a
host program chosen by whoever can write ``.git/config`` (a model, or a cloned
hostile repository).  Each tool must run git with those neutralized, exactly
like the harness mutation tools do.
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
import sonder_runtime.adapters.filesystem.file_ops as file_ops


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _marker_program(tmp_path: Path, marker: Path, *, echo_stdin: bool) -> str:
    """A Python program (works on Windows and POSIX) that records it ran."""
    script = tmp_path / ("echo-%s.py" % marker.stem)
    body = (
        "import pathlib, sys\n"
        "with open(%r, 'a', encoding='utf-8') as handle:\n"
        "    handle.write('ran %%r\\n' %% (sys.argv[1:],))\n" % str(marker)
    )
    if echo_stdin:
        body += "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    script.write_text(body, encoding="utf-8")
    return '"%s" "%s"' % (
        str(sys.executable).replace("\\", "/"), str(script).replace("\\", "/"),
    )


@pytest.fixture
def hostile_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Sonder Tests")
    _git(repo, "config", "user.email", "sonder-tests@example.invalid")
    (repo / ".gitattributes").write_text(
        "*.txt filter=hostile diff=hostile\n", encoding="utf-8",
    )
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")

    _git(repo, "config", "core.fsmonitor",
         _marker_program(tmp_path, tmp_path / "fsmonitor.txt", echo_stdin=False))
    _git(repo, "config", "filter.hostile.clean",
         _marker_program(tmp_path, tmp_path / "clean.txt", echo_stdin=True))
    _git(repo, "config", "filter.hostile.required", "true")
    _git(repo, "config", "diff.hostile.textconv",
         _marker_program(tmp_path, tmp_path / "textconv.txt", echo_stdin=False))
    _git(repo, "config", "diff.hostile.command",
         _marker_program(tmp_path, tmp_path / "diffcmd.txt", echo_stdin=False))
    _git(repo, "config", "diff.external",
         _marker_program(tmp_path, tmp_path / "external.txt", echo_stdin=False))

    # Same bytes, newer mtime: git must re-hash the file (through the clean
    # filter) to learn whether it changed.
    tracked = repo / "tracked.txt"
    tracked.write_text("second\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(tracked, (later, later))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: repo)
    return repo, tmp_path


def _ran(tmp_path: Path) -> dict:
    return {
        name: (tmp_path / ("%s.txt" % name)).read_text(encoding="utf-8")
        for name in ("fsmonitor", "clean", "textconv", "diffcmd", "external")
        if (tmp_path / ("%s.txt" % name)).exists()
    }


def test_planted_programs_do_run_under_plain_git(hostile_repo):
    """Guard against a vacuous GREEN: the plant must fire without the fix."""
    repo, tmp_path = hostile_repo
    _git(repo, "status", "--porcelain")
    _git(repo, "-c", "filter.hostile.required=false", "diff", "HEAD~1", "HEAD")
    ran = _ran(tmp_path)
    assert "fsmonitor" in ran and "clean" in ran, ran
    assert "external" in ran or "diffcmd" in ran, ran


def test_repo_status_runs_no_repository_program(hostile_repo):
    repo, tmp_path = hostile_repo
    result = git_tools.repo_status(str(repo), bypass=True)
    assert result["branch"] == "main"
    assert _ran(tmp_path) == {}


@pytest.mark.parametrize("staged", [False, True])
def test_repo_diff_runs_no_repository_program(hostile_repo, staged):
    repo, tmp_path = hostile_repo
    (repo / "tracked.txt").write_text("third\n", encoding="utf-8")
    if staged:
        _git(repo, "-c", "filter.hostile.clean=cat", "-c", "core.fsmonitor=false",
             "add", "tracked.txt")
    result = git_tools.repo_diff(str(repo), bypass=True, staged=staged)
    assert "+third" in result["diff"]
    assert _ran(tmp_path) == {}


def test_git_history_tools_run_no_repository_program(hostile_repo):
    repo, tmp_path = hostile_repo
    log = git_history.repo_log(repo, file_path="tracked.txt")
    assert [row["subject"] for row in log["commits"]] == ["second", "initial"]
    show = git_history.repo_show(repo, file_path="tracked.txt")
    assert "+second" in show["patch"]
    blame = git_history.repo_blame(repo, file_path="tracked.txt")
    assert blame["count"] == 1
    assert _ran(tmp_path) == {}


def test_git_tools_command_carries_the_shared_neutralization(hostile_repo, monkeypatch):
    repo, _tmp_path = hostile_repo
    real_popen = git_tools.subprocess.Popen
    commands = []

    def observed(command, **kwargs):
        commands.append(list(command))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(git_tools.subprocess, "Popen", observed)
    git_tools.repo_status(str(repo), bypass=True)
    status = next(command for command in commands if "status" in command)
    assert "core.fsmonitor=false" in status
    assert "core.hooksPath=" + os.devnull in status
    assert "filter.hostile.clean=cat" in status
    assert "diff.hostile.textconv=cat" in status


def test_git_tools_refuse_when_driver_config_cannot_be_read(hostile_repo, monkeypatch):
    repo, tmp_path = hostile_repo
    real_spawn = git_tools._spawn_git

    def failing_probe(root, arguments, **kwargs):
        if arguments[:1] == ["config"]:
            return {**real_spawn(root, ["--version"], **kwargs), "returncode": 128,
                    "stderr": "fatal: bad config"}
        return real_spawn(root, arguments, **kwargs)

    monkeypatch.setattr(git_tools, "_spawn_git", failing_probe)
    with pytest.raises(PermissionError, match="refusing to run git"):
        git_tools.repo_status(str(repo), bypass=True)
    assert _ran(tmp_path) == {}


def test_guard_rejects_unexpected_driver_keys():
    from sonder_runtime.adapters import git_program_guard

    with pytest.raises(git_program_guard.GitProgramConfigError):
        git_program_guard.driver_overrides(["filter.x.unexpected"])
    overrides = git_program_guard.driver_overrides([
        "filter.a.clean", "FILTER.a.smudge", "merge.m.driver", "diff.d.textconv",
    ])
    assert overrides.count("filter.a.clean=cat") == 1
    assert "merge.m.driver=" + git_program_guard.BUILTIN_MERGE_DRIVER in overrides
    assert "diff.d.textconv=cat" in overrides
