"""Issue #510 guard canaries: concurrent Git mutation on one working tree.

Each canary deliberately trips ``git_mutation_guard`` through the production
entry points (``harness_tools`` git mutations, the ``server`` MCP wrappers,
and runtime-source maintenance) and asserts the blocked mutation never reached
Git.  The normal-traffic tests prove sequential mutations and mutations on
different working trees are not blocked.
"""
from __future__ import annotations

import subprocess
import threading

import pytest

import git_tools
import harness_tools
from sonder_runtime.adapters import git_mutation_guard as guard


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    # Authorize pytest's tmp tree exactly as tests/test_harness_git.py does;
    # root confinement itself stays enforced.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))
    guard.reset_for_tests()
    yield
    guard.reset_for_tests()


def _fake_tree(path):
    (path / ".git").mkdir(parents=True)
    return path


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


class _BlockingGit:
    """Stands in for ``harness_tools._run_git``; the first call blocks."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, root, args, *, timeout=10):
        self.calls.append(list(args))
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(10), "canary holder was never released"
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}


def _hold_commit(root, fake):
    result = {}

    def run():
        result["value"] = harness_tools.git_commit(
            root=str(root), message="held", all_tracked=False,
        )

    thread = threading.Thread(target=run, name="canary-holder")
    thread.start()
    assert fake.entered.wait(10)
    return thread, result


def test_canary_second_mutation_on_same_tree_is_refused_without_running_git(
    tmp_path, monkeypatch,
):
    root = _fake_tree(tmp_path / "repo")
    fake = _BlockingGit()
    monkeypatch.setattr(harness_tools, "_run_git", fake)
    monkeypatch.setattr(guard, "DEFAULT_WAIT_SECONDS", 0.05)
    thread, held = _hold_commit(root, fake)
    try:
        # A different mutation, from a subdirectory of the same tree.
        (root / "sub").mkdir()
        refused = harness_tools.git_checkout(root=str(root / "sub"), ref="other")
    finally:
        fake.release.set()
        thread.join(10)

    assert refused["ok"] is False
    assert refused["guard"] == guard.GUARD_NAME
    assert refused["guard_reason"] == "busy"
    assert refused["holder"] == "git_commit"
    assert "HOST GUARD" in refused["error"]
    assert "repo_status" in refused["recovery"]
    # Only the holder's commit reached Git; the checkout never ran.
    assert fake.calls == [["commit", "-m", "held"]]
    assert held["value"]["ok"] is True
    snapshot = guard.guard_snapshot()
    assert snapshot["refused"] == 1 and snapshot["active"] == {}


def test_canary_mcp_wrapper_reports_the_refusal(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    root = _fake_tree(tmp_path / "repo")
    fake = _BlockingGit()
    monkeypatch.setattr(harness_tools, "_run_git", fake)
    monkeypatch.setattr(guard, "DEFAULT_WAIT_SECONDS", 0.05)
    thread, _held = _hold_commit(root, fake)
    try:
        output = server.git_stash(root=str(root), action="pop")
    finally:
        fake.release.set()
        thread.join(10)

    assert "HOST GUARD" in output
    assert "git_commit" in output
    assert len(fake.calls) == 1


def test_canary_foreign_index_lock_is_reported_not_deleted(tmp_path, monkeypatch):
    root = _fake_tree(tmp_path / "repo")
    lock = root / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        harness_tools, "_run_git",
        lambda *a, **k: calls.append(a) or {"ok": True},
    )

    refused = harness_tools.git_merge(root=str(root), branch="feature")

    assert refused["ok"] is False
    assert refused["guard_reason"] == "foreign_index_lock"
    assert calls == []
    assert lock.exists(), "the host must never delete a lock it did not create"


def test_canary_runtime_update_is_refused_while_a_tool_mutation_runs(
    tmp_path, monkeypatch,
):
    import server

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    root = _fake_tree(tmp_path / "runtime")
    monkeypatch.setattr(server, "_runtime_source_root", lambda: str(root))
    probes = []
    monkeypatch.setattr(
        git_tools, "_require_repository_root",
        lambda *a, **k: probes.append(a) or root,
    )
    monkeypatch.setattr(guard, "DEFAULT_WAIT_SECONDS", 0.05)
    fake = _BlockingGit()
    monkeypatch.setattr(harness_tools, "_run_git", fake)
    thread, _held = _hold_commit(root, fake)
    try:
        with pytest.raises(guard.ConcurrentGitMutation):
            git_tools.runtime_update(root)
        output = server.runtime_source_update()
        stash_output = server.runtime_source_stash("save")
    finally:
        fake.release.set()
        thread.join(10)

    assert output.startswith("runtime source update refused: HOST GUARD")
    assert stash_output.startswith("runtime source stash refused: HOST GUARD")
    # Neither maintenance path got as far as its status probe.
    assert probes == []


def test_normal_sequential_mutations_on_a_real_repository_are_not_blocked(tmp_path):
    root = _init_repo(tmp_path / "repo")

    branch = harness_tools.git_branch(root=str(root), name="feature")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    commit = harness_tools.git_commit(
        root=str(root), message="add b", paths_json='["b.txt"]',
    )
    back = harness_tools.git_checkout(root=str(root), ref="master")

    assert branch["ok"] is True, branch
    assert commit["ok"] is True, commit
    assert back["ok"] is True, back
    snapshot = guard.guard_snapshot()
    assert snapshot["admitted"] == 3
    assert snapshot["refused"] == 0


def test_normal_mutations_on_different_trees_run_concurrently(tmp_path, monkeypatch):
    held_root = _fake_tree(tmp_path / "one")
    other_root = _fake_tree(tmp_path / "two")
    fake = _BlockingGit()
    monkeypatch.setattr(harness_tools, "_run_git", fake)
    monkeypatch.setattr(guard, "DEFAULT_WAIT_SECONDS", 0.05)
    thread, _held = _hold_commit(held_root, fake)
    try:
        other = harness_tools.git_checkout(root=str(other_root), ref="x")
    finally:
        fake.release.set()
        thread.join(10)

    assert other["ok"] is True
    assert fake.calls[1] == ["checkout", "x"]


def test_slot_is_released_when_the_guarded_mutation_raises(tmp_path, monkeypatch):
    root = _fake_tree(tmp_path / "repo")

    def boom(*a, **k):
        raise RuntimeError("git crashed")

    monkeypatch.setattr(harness_tools, "_run_git", boom)
    with pytest.raises(RuntimeError):
        harness_tools.git_tag(root=str(root), name="v1")
    monkeypatch.setattr(
        harness_tools, "_run_git", lambda *a, **k: {"ok": True},
    )
    assert harness_tools.git_tag(root=str(root), name="v1")["ok"] is True


def test_linked_worktree_is_its_own_key(tmp_path):
    main = _fake_tree(tmp_path / "main")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(
        "gitdir: %s\n" % (main / ".git" / "worktrees" / "linked"), encoding="utf-8",
    )
    assert guard.worktree_key(linked / "deep") == guard.worktree_key(linked)
    assert guard.worktree_key(linked) != guard.worktree_key(main)


# -- review follow-ups (PR #553) --------------------------------------------

def test_canary_mcp_wrapper_surfaces_guard_and_recovery_fields(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    root = _fake_tree(tmp_path / "repo")
    fake = _BlockingGit()
    monkeypatch.setattr(harness_tools, "_run_git", fake)
    monkeypatch.setattr(guard, "DEFAULT_WAIT_SECONDS", 0.05)
    thread, _held = _hold_commit(root, fake)
    try:
        output = server.git_merge(root=str(root), branch="feature")
    finally:
        fake.release.set()
        thread.join(10)

    assert "  guard: git_mutation_concurrency (busy)" in output
    assert "  recovery: Do not retry this mutation blindly." in output
    assert "re-inspect the tree with repo_status" in output


def test_index_lock_refusal_does_not_assume_a_crash_or_writer(tmp_path, monkeypatch):
    root = _fake_tree(tmp_path / "repo")
    (root / ".git" / "index.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(harness_tools, "_run_git", lambda *a, **k: {"ok": True})

    refused = harness_tools.git_commit(root=str(root), message="m")

    assert (
        "another git process holds index.lock (possibly a concurrent read "
        "or a crashed process)" in refused["error"]
    )


def test_canary_linked_worktree_leftover_index_lock_is_refused_not_deleted(
    tmp_path, monkeypatch,
):
    main = _fake_tree(tmp_path / "main")
    private = main / ".git" / "worktrees" / "linked"
    private.mkdir(parents=True)
    lock = private / "index.lock"
    lock.write_text("", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: %s\n" % private, encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        harness_tools, "_run_git",
        lambda *a, **k: calls.append(a) or {"ok": True},
    )

    refused = harness_tools.git_checkout(root=str(linked), ref="other")
    # The main worktree's own index is free, so it is not blocked.
    admitted = harness_tools.git_checkout(root=str(main), ref="other")

    assert refused["ok"] is False
    assert refused["guard_reason"] == "foreign_index_lock"
    assert str(lock) in refused["error"]
    assert lock.exists()
    assert admitted["ok"] is True
    assert len(calls) == 1


def test_linked_worktree_relative_gitdir_leftover_lock_is_refused(tmp_path, monkeypatch):
    main = _fake_tree(tmp_path / "main")
    private = main / ".git" / "worktrees" / "rel"
    private.mkdir(parents=True)
    (private / "index.lock").write_text("", encoding="utf-8")
    linked = tmp_path / "rel"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../main/.git/worktrees/rel\n", encoding="utf-8")
    monkeypatch.setattr(harness_tools, "_run_git", lambda *a, **k: {"ok": True})

    refused = harness_tools.git_stash(root=str(linked), action="push")

    assert refused["guard_reason"] == "foreign_index_lock"
