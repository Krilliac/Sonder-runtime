"""Tests for harness_tools git mutation functions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import harness_tools


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    """Authorize pytest's tmp tree for ``harness_tools._resolve_root``.

    ``_resolve_root`` now confines every caller-supplied ``root`` to
    ``file_ops.allowed_roots()`` -- see its docstring for why. These tests
    legitimately do their work in ``tmp_path``, so they authorize exactly that
    tree, the same way an operator authorizes a repository in
    ``file_roots.local``.

    This AUTHORIZES a root; it does not disable the check. A root outside the
    tmp tree is still refused, which is what
    ``tests/test_harness_root_confinement.py`` asserts -- without that file
    this fixture would be indistinguishable from deleting the guard.
    """
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))


def _init_repo(tmp_path):
    """Create a git repo with one committed file and return root path."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "hello.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    return tmp_path


# ── git_commit ──────────────────────────────────────────────────────────


def test_git_commit_empty_message_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_commit(root=str(tmp_path), message="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_git_commit_with_paths_json(tmp_path):
    root = _init_repo(tmp_path)
    (root / "new.txt").write_text("new file\n", encoding="utf-8")
    result = harness_tools.git_commit(
        root=str(root),
        message="add new.txt",
        paths_json=json.dumps(["new.txt"]),
    )
    assert result["ok"] is True
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--oneline"],
        capture_output=True, text=True,
    )
    assert "add new.txt" in log.stdout


def test_git_commit_all_tracked(tmp_path):
    root = _init_repo(tmp_path)
    (root / "hello.txt").write_text("modified\n", encoding="utf-8")
    result = harness_tools.git_commit(
        root=str(root), message="update tracked", all_tracked=True,
    )
    assert result["ok"] is True
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--oneline"],
        capture_output=True, text=True,
    )
    assert "update tracked" in log.stdout


# ── git_branch ──────────────────────────────────────────────────────────


def test_git_branch_missing_name_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_branch(root=str(tmp_path), name="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_git_branch_create_and_checkout(tmp_path):
    root = _init_repo(tmp_path)
    result = harness_tools.git_branch(root=str(root), name="feature-x", checkout=True)
    assert result["ok"] is True
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        capture_output=True, text=True,
    )
    assert branch.stdout.strip() == "feature-x"


def test_git_branch_create_without_checkout(tmp_path):
    root = _init_repo(tmp_path)
    result = harness_tools.git_branch(root=str(root), name="side", checkout=False)
    assert result["ok"] is True
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        capture_output=True, text=True,
    )
    assert branch.stdout.strip() != "side"
    branches = subprocess.run(
        ["git", "-C", str(root), "branch"],
        capture_output=True, text=True,
    )
    assert "side" in branches.stdout


def test_git_branch_with_base(tmp_path):
    root = _init_repo(tmp_path)
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "b.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "second"],
        check=True,
    )
    initial_sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD~1"],
        capture_output=True, text=True,
    ).stdout.strip()
    result = harness_tools.git_branch(
        root=str(root), name="from-initial", checkout=True, base=initial_sha,
    )
    assert result["ok"] is True
    assert not (root / "b.txt").exists() or subprocess.run(
        ["git", "-C", str(root), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout.count("\n") == 1


# ── git_checkout ────────────────────────────────────────────────────────


def test_git_checkout_missing_ref_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_checkout(root=str(tmp_path), ref="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_git_checkout_existing_branch(tmp_path):
    root = _init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(root), "branch", "other"], check=True,
    )
    result = harness_tools.git_checkout(root=str(root), ref="other")
    assert result["ok"] is True
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        capture_output=True, text=True,
    )
    assert branch.stdout.strip() == "other"


# ── git_stash ───────────────────────────────────────────────────────────


def test_git_stash_push_and_pop(tmp_path):
    root = _init_repo(tmp_path)
    (root / "hello.txt").write_text("dirty\n", encoding="utf-8")
    push = harness_tools.git_stash(root=str(root), action="push", message="wip")
    assert push["ok"] is True
    assert (root / "hello.txt").read_text(encoding="utf-8") == "hello\n"

    pop = harness_tools.git_stash(root=str(root), action="pop")
    assert pop["ok"] is True
    assert (root / "hello.txt").read_text(encoding="utf-8") == "dirty\n"


def test_git_stash_list(tmp_path):
    root = _init_repo(tmp_path)
    result = harness_tools.git_stash(root=str(root), action="list")
    assert result["ok"] is True


def test_git_stash_unknown_action(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_stash(root=str(tmp_path), action="invalid")
    assert result["ok"] is False
    assert "unknown" in result["error"]


# ── git_tag ─────────────────────────────────────────────────────────────


def test_git_tag_missing_name_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_tag(root=str(tmp_path), name="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_git_tag_lightweight(tmp_path):
    root = _init_repo(tmp_path)
    result = harness_tools.git_tag(root=str(root), name="v1.0")
    assert result["ok"] is True
    tags = subprocess.run(
        ["git", "-C", str(root), "tag"],
        capture_output=True, text=True,
    )
    assert "v1.0" in tags.stdout


def test_git_tag_annotated(tmp_path):
    root = _init_repo(tmp_path)
    result = harness_tools.git_tag(root=str(root), name="v2.0", message="release 2")
    assert result["ok"] is True
    show = subprocess.run(
        ["git", "-C", str(root), "tag", "-n1", "v2.0"],
        capture_output=True, text=True,
    )
    assert "release 2" in show.stdout


def test_git_tag_delete(tmp_path):
    root = _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "tag", "old"], check=True)
    result = harness_tools.git_tag(root=str(root), name="old", delete=True)
    assert result["ok"] is True
    tags = subprocess.run(
        ["git", "-C", str(root), "tag"],
        capture_output=True, text=True,
    )
    assert "old" not in tags.stdout


# ── git_merge ───────────────────────────────────────────────────────────


def test_git_merge_missing_branch_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_merge(root=str(tmp_path), branch="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_git_merge_branch(tmp_path):
    root = _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "feat"], check=True)
    (root / "feat.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "feat.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "feat commit"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "master"], check=True)
    result = harness_tools.git_merge(
        root=str(root), branch="feat", message="merge feat",
    )
    assert result["ok"] is True
    assert (root / "feat.txt").exists()


# ── git_cherry_pick ─────────────────────────────────────────────────────


def test_git_cherry_pick_empty_commits_returns_error(tmp_path):
    _init_repo(tmp_path)
    result = harness_tools.git_cherry_pick(root=str(tmp_path), commits_json="[]")
    assert result["ok"] is False
    assert "non-empty" in result["error"]


def test_git_cherry_pick_commit(tmp_path):
    root = _init_repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "src"], check=True)
    (root / "pick.txt").write_text("picked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "pick.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "to pick"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "master"], check=True)
    result = harness_tools.git_cherry_pick(
        root=str(root), commits_json=json.dumps([sha]),
    )
    assert result["ok"] is True
    assert (root / "pick.txt").exists()
