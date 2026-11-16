"""Shared fixtures: a runtime checkout that holds the model tool policy files.

``policy_files`` builds a package data directory (holding a copy of the shipped
policy) and an operator overlay, both under ``tmp_path`` inside one directory
(``repo``); the real shipped file is never the target.  ``checkout`` makes that
directory a git repository, so the git tools treat it as the live runtime
checkout (it holds the package data directory).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters.filesystem import file_ops

PROTECTED = "control-plane|control state|hard link|protected"
FORGED = json.dumps({"version": 1, "model_profiles": [
    {"id": "forged", "match": {"names": [], "prefixes": ["astra-cyber-"]},
     "tools": ["text_search"], "lanes": ["agent"]},
]}) + "\n"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path namespaces")


@pytest.fixture
def policy_files(monkeypatch, tmp_path):
    """A package data directory (with the shipped policy) and an overlay, both under tmp_path."""
    repo = tmp_path / "checkout"
    data = repo / "pkg" / "data"
    data.mkdir(parents=True)
    shipped = data / "model_tool_policy.json"
    shutil.copyfile(mtp.SHIPPED_PATH, shipped)
    overlay = repo / "state" / mtp.OVERLAY_FILE_NAME
    overlay.parent.mkdir()
    overlay.write_text('{"version": 1, "model_profiles": []}\n', encoding="utf-8")
    monkeypatch.setattr(file_ops, "RUNTIME_PACKAGE_DATA_DIR", data)
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(overlay))
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    (repo / "proj").mkdir()
    assert file_ops._is_protected_mutation_path(shipped)
    assert file_ops._is_protected_mutation_path(overlay)
    return SimpleNamespace(repo=repo, data=data, shipped=shipped, overlay=overlay, project=repo / "proj")


def targets(files):
    return {"shipped": files.shipped, "overlay": files.overlay}


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


@pytest.fixture
def checkout(policy_files):
    """The live checkout as a git repository, with the policy files committed."""
    repo = policy_files.repo
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "test@test.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return policy_files


def evil_branch(files, name="evil"):
    """A branch that rewrites both policy files, then back on master; returns its commit id."""
    repo = files.repo
    git(repo, "checkout", "-q", "-b", name)
    files.shipped.write_text(FORGED, encoding="utf-8")
    files.overlay.write_text(FORGED, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "grant everything")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "master")
    return sha


def plain_branch(files, name="plain"):
    """A branch that changes only hello.txt, then back on master; returns its commit id."""
    repo = files.repo
    git(repo, "checkout", "-q", "-b", name)
    (repo / "hello.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "plain change")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "master")
    return sha
