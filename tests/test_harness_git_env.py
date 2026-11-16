"""The git tools run git on the tree that was checked, whatever the runtime's environment says.

``GIT_DIR``, ``GIT_WORK_TREE``, ``GIT_INDEX_FILE`` and ``GIT_COMMON_DIR`` would
point git at another repository than the one the live-checkout guard judged;
``GIT_OBJECT_DIRECTORY`` and ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` at another
object store; ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` /
``GIT_CONFIG_VALUE_<n>`` and ``GIT_CONFIG_PARAMETERS`` inject config entries
(a hook path, an fsmonitor program) past the ``-c`` overrides.  ``_git_env``
drops all of them, in any letter case, and keeps the rest.
"""
from __future__ import annotations

import os

import pytest

import harness_tools

STRIPPED = [
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_12", "GIT_CONFIG_VALUE_12",
    "GIT_CONFIG_PARAMETERS",
]


@pytest.fixture
def child_env(monkeypatch):
    """``_child_env`` returning a fixed environment, so the test sees exactly what ``_git_env`` removes."""
    base = {name: "x" for name in STRIPPED}
    base.update({"git_config_key_1": "lower", "PATH": "p", "GIT_AUTHOR_NAME": "kept", "HOME": "h"})
    monkeypatch.setattr(harness_tools, "_child_env", lambda: dict(base))
    return base


def test_git_env_drops_every_variable_that_redirects_git(child_env):
    env = harness_tools._git_env()
    for name in STRIPPED + ["git_config_key_1"]:
        assert name not in env, name
    assert env["PATH"] == "p" and env["HOME"] == "h" and env["GIT_AUTHOR_NAME"] == "kept"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_an_injected_config_entry_does_not_reach_git(monkeypatch, tmp_path):
    """End to end: a ``GIT_CONFIG_*`` entry in the runtime's environment is not seen by the git a tool runs."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "sonder.injected")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "yes")
    repo = tmp_path / "repo"
    repo.mkdir()
    result = harness_tools._run(
        [harness_tools._git(), "init", "-q", str(repo)], cwd=tmp_path, timeout=30, env=harness_tools._git_env(),
    )
    assert result["ok"] is True, result
    probe = harness_tools._run(
        [harness_tools._git(), "config", "--get", "sonder.injected"], cwd=repo, timeout=30,
        env=harness_tools._git_env(),
    )
    assert probe["returncode"] != 0 and "yes" not in probe["stdout"], probe
    # Control: the same probe with the environment left as is does see the entry.
    seen = harness_tools._run(
        [harness_tools._git(), "config", "--get", "sonder.injected"], cwd=repo, timeout=30, env=dict(os.environ),
    )
    assert "yes" in seen["stdout"], seen
