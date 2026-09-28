"""Harness git mutations must not run programs named by repository config.

``git_commit``/``git_checkout``/``git_stash``/``git_merge``/``git_cherry_pick``
are graded ``mutation``, which ``acceptEdits`` allows unattended, while
running host programs still asks. A repository's own ``.git/hooks``,
``core.fsmonitor``, filter, merge-driver or signing program is exactly such a
host program, and a model (or an untrusted checkout) controls it. The harness
now runs git with those neutralized.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import harness_tools


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "master", str(root)], check=True)
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")
    (root / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "hello.txt")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _marker_command(marker: Path) -> str:
    # Runs through git's shell on every platform (Git for Windows ships sh).
    return "sh -c 'echo ran >> \"%s\"'" % marker.as_posix()


def _plant(root: Path, marker: Path) -> None:
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for hook in ("pre-commit", "commit-msg", "post-commit", "post-checkout",
                 "post-merge", "pre-merge-commit", "prepare-commit-msg"):
        script = hooks / hook
        script.write_text(
            "#!/bin/sh\necho %s >> \"%s\"\n" % (hook, marker.as_posix()),
            encoding="utf-8", newline="\n",
        )
        script.chmod(0o755)
    command = _marker_command(marker)
    _git(root, "config", "core.fsmonitor", command)
    _git(root, "config", "filter.evil.clean", command)
    _git(root, "config", "filter.evil.smudge", command)
    _git(root, "config", "merge.evil.driver", command)
    _git(root, "config", "commit.gpgSign", "true")
    _git(root, "config", "gpg.program", command)
    (root / ".gitattributes").write_text("*.txt filter=evil merge=evil\n", encoding="utf-8")


def test_git_commit_does_not_run_repository_hooks_or_drivers(tmp_path):
    root = _init_repo(tmp_path / "repo")
    marker = tmp_path / "marker.txt"
    _plant(root, marker)
    (root / "hello.txt").write_text("changed\n", encoding="utf-8")

    result = harness_tools.git_commit(
        str(root), message="update", all_tracked=True,
    )

    assert result["ok"], result
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s", "-1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert log == "update"


def test_git_checkout_and_merge_do_not_run_repository_programs(tmp_path):
    root = _init_repo(tmp_path / "repo")
    _git(root, "checkout", "-q", "-b", "side")
    (root / "side.txt").write_text("side\n", encoding="utf-8")
    _git(root, "add", "side.txt")
    _git(root, "commit", "-q", "-m", "side")
    _git(root, "checkout", "-q", "master")
    marker = tmp_path / "marker.txt"
    _plant(root, marker)

    checkout = harness_tools.git_checkout(str(root), ref="side")
    assert checkout["ok"], checkout
    back = harness_tools.git_checkout(str(root), ref="master")
    assert back["ok"], back
    merged = harness_tools.git_merge(str(root), branch="side", message="merge side")
    assert merged["ok"], merged
    assert not marker.exists(), marker.read_text(encoding="utf-8")


def test_unreadable_driver_configuration_fails_closed(tmp_path, monkeypatch):
    root = _init_repo(tmp_path / "repo")
    real_run = harness_tools._run

    def probe_fails(cmd, **kwargs):
        if "--get-regexp" in cmd:
            return {"ok": False, "returncode": 128, "timed_out": False,
                    "elapsed_ms": 0, "stdout": "", "stderr": "bad config",
                    "command": list(cmd)}
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(harness_tools, "_run", probe_fails)
    (root / "hello.txt").write_text("changed\n", encoding="utf-8")
    result = harness_tools.git_commit(str(root), message="x", all_tracked=True)
    assert result["ok"] is False
    assert "driver configuration" in result["stderr"]
