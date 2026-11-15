"""Round-3 rule: an unattended git or patch mutation never runs in the live runtime checkout.

The live checkout is the repository that holds the runtime's control-plane
locations (the shipped policy's package data directory, the overlay).  A
model that can run ``git_checkout``, ``git_branch`` with a base, ``git_merge``,
``git_cherry_pick``, ``git_stash``, ``apply_patch`` or ``format_code`` there
can swap the shipped model tool policy for another commit's, however the ref
is spelled (a branch named like a commit id, a local branch named
``refs/remotes/...``).  The rule is not "check the ref"; it is "no such tool
runs in the live checkout unless the console operator typed it":

* ``operator_attendance`` is a context variable bound to one tool name, set
  only by the console's ``/<tool>`` dispatcher while a person is present;
* the guard runs no git at all (nothing to resolve, nothing to mis-resolve);
* a repository that is not the live checkout (a user's repository, a git
  worktree of the runtime, a nested repository) keeps every behaviour.

Each refusal is paired with a control showing the same call works elsewhere.
"""
from __future__ import annotations

import json
import subprocess

import pytest

import harness_tools
import sonder_runtime.interfaces.repl.repl as sonder_repl
from tests.support.policy_checkout import (
    evil_branch,
    git,
    plain_branch,
    windows_only,
)

pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files and checkout fixtures


def _patch(relative, old, new):
    return f"--- a/{relative}\n+++ b/{relative}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def _head(files):
    return git(files.repo, "rev-parse", "HEAD").stdout.strip()


def _branch(files):
    return git(files.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _hello(files):
    return (files.repo / "hello.txt").read_text(encoding="utf-8")


def _refused(result):
    assert result.get("ok") is False and "protected" in str(result.get("error", "")), str(result)[:500]


@pytest.fixture
def no_child(monkeypatch):
    calls = []

    def run(cmd, *, cwd, timeout=None, **kwargs):
        calls.append((list(cmd), cwd))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(harness_tools, "_run", run)
    return calls


@pytest.fixture
def attended_console(monkeypatch):
    """The REPL as a person at the console sees it: an operator is present and the gate says yes."""
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_permission_gate", lambda tool, arguments=None: (True, ""))


@pytest.fixture
def unattended_console(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(sonder_repl, "_permission_gate", lambda tool, arguments=None: (True, ""))


# ---- the bypasses: a ref that the old per-ref check resolved differently from git ------

@pytest.mark.parametrize("tool", ["git_checkout", "git_branch"])
def test_hex_named_branch_cannot_swap_the_tree(checkout, tool):
    """A branch named like HEAD's commit id (pointing at a forged tree) wins over the id in git."""
    forged = evil_branch(checkout)
    head = _head(checkout)
    git(checkout.repo, "branch", head, forged)  # raw git: refs/heads/<HEAD's id> -> the forged commit
    good = checkout.shipped.read_bytes()
    if tool == "git_checkout":
        result = harness_tools.git_checkout(str(checkout.repo), "HEAD")
    else:
        result = harness_tools.git_branch(str(checkout.repo), "work", base="HEAD")
    _refused(result)
    assert checkout.shipped.read_bytes() == good
    assert _head(checkout) == head and _branch(checkout) == "master"


@pytest.mark.parametrize("tool", ["git_checkout", "git_branch", "git_branch_no_checkout"])
def test_a_local_branch_named_refs_remotes_cannot_swap_the_tree(checkout, tool):
    """``origin/main`` and a local branch spelled ``refs/remotes/origin/main`` name different commits.

    Whatever the tool does with the ambiguity, the live working tree and HEAD never move to the
    forged commit.
    """
    forged = evil_branch(checkout)
    good_sha = _head(checkout)
    git(checkout.repo, "update-ref", "refs/remotes/origin/main", good_sha)
    git(checkout.repo, "branch", "refs/remotes/origin/main", forged)
    good = checkout.shipped.read_bytes()
    root = str(checkout.repo)
    if tool == "git_checkout":
        result = harness_tools.git_checkout(root, "origin/main")
    elif tool == "git_branch":
        result = harness_tools.git_branch(root, "work", base="origin/main")
    else:
        result = harness_tools.git_branch(root, "work", checkout=False, base="origin/main")
    assert checkout.shipped.read_bytes() == good, str(result)[:500]
    assert checkout.overlay.read_text(encoding="utf-8") == '{"version": 1, "model_profiles": []}\n'
    assert _head(checkout) == good_sha, str(result)[:500]  # never the forged commit


# ---- every tool that rewrites the live working tree, however harmless the change ---------

@pytest.mark.parametrize("spelling", [
    "plain",
    pytest.param(":/plain change", id="by-message"),
])
@pytest.mark.parametrize("tool", ["git_merge", "git_cherry_pick"])
def test_an_unattended_merge_or_cherry_pick_in_the_live_checkout_is_refused(checkout, tool, spelling):
    plain = plain_branch(checkout)
    head = _head(checkout)
    root = str(checkout.repo)
    if tool == "git_merge":
        result = harness_tools.git_merge(root, spelling)
    else:
        result = harness_tools.git_cherry_pick(root, json.dumps([plain if spelling == "plain" else spelling]))
    _refused(result)
    assert _head(checkout) == head and _hello(checkout) == "hello\n"


def _checkout_plain(files, root):
    return harness_tools.git_checkout(root, "plain"), "hello\n"


def _stash_push(files, root):
    (files.repo / "hello.txt").write_text("edited\n", encoding="utf-8")
    return harness_tools.git_stash(root, action="push", include_untracked=False), "edited\n"


def _stash_pop(files, root):
    (files.repo / "hello.txt").write_text("edited\n", encoding="utf-8")
    git(files.repo, "stash", "push", "-q")  # stashed outside the tools
    return harness_tools.git_stash(root, action="pop"), "hello\n"


def _apply_patch(files, root):
    return harness_tools.apply_patch(root, _patch("hello.txt", "hello", "hi")), "hello\n"


def _branch_with_base(files, root):
    return harness_tools.git_branch(root, "mine", base="plain"), "hello\n"


@pytest.mark.parametrize("mutate", [
    _checkout_plain, _stash_push, _stash_pop, _apply_patch, _branch_with_base,
], ids=lambda f: f.__name__.lstrip("_"))
def test_an_unattended_git_or_patch_mutation_in_the_live_checkout_is_refused(checkout, mutate):
    plain_branch(checkout)
    head, branch = _head(checkout), _branch(checkout)
    result, expected_hello = mutate(checkout, str(checkout.repo))
    _refused(result)
    # Nothing moved: same commit, same branch, hello.txt as the helper left it.
    assert _head(checkout) == head and _branch(checkout) == branch
    assert _hello(checkout) == expected_hello
    assert git(checkout.repo, "show-ref", "--verify", "--quiet", "refs/heads/mine", check=False).returncode != 0


def test_formatter_in_the_live_checkout_does_not_run_unattended(checkout, no_child):
    result = harness_tools.format_code(str(checkout.repo), tool="ruff", path="proj")
    _refused(result)
    assert no_child == []
    result = harness_tools.lint_run(str(checkout.repo), tool="ruff", path="proj", fix=True)
    _refused(result)
    assert no_child == []
    # Control: a check-only formatter pass changes nothing and still runs.
    assert harness_tools.format_code(str(checkout.repo), tool="ruff", path="proj", check_only=True)["ok"] is True
    assert len(no_child) == 1


def test_the_live_guard_runs_no_git(checkout, monkeypatch, tmp_path):
    """The guard decides from paths alone: no ``git`` child runs before the refusal."""
    plain_branch(checkout)
    calls = []
    real = subprocess.run

    def recording(argv, *args, **kwargs):
        calls.append(list(argv) if not isinstance(argv, str) else [argv])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    _refused(harness_tools.git_checkout(str(checkout.repo), "plain"))
    assert calls == []
    # Control: in a user's repository the same tool runs git (so the recorder does see calls).
    repo = tmp_path / "user_repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "test@test.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "branch", "other")
    del calls[:]
    assert harness_tools.git_checkout(str(repo), "other")["ok"] is True
    assert calls
    # ... and no ``rev-parse --show-toplevel`` probe ran to decide whether the repository is live.
    assert not [argv for argv in calls if "--show-toplevel" in argv], calls


# ---- the operator at the console is not locked out --------------------------------------

def _console(files, line, tool):
    return sonder_repl._run_catalogued(line.replace("{repo}", files.repo.as_posix()), "/" + tool)


def test_an_attended_console_git_and_patch_tools_work(checkout, attended_console):
    plain = plain_branch(checkout)
    result = _console(checkout, "/git_checkout root={repo} ref=plain", "git_checkout")
    assert _head(checkout) == plain, result
    assert _branch(checkout) == "plain"
    _console(checkout, "/git_checkout root={repo} ref=master", "git_checkout")
    assert _branch(checkout) == "master"
    # stash push / pop
    (checkout.repo / "hello.txt").write_text("edited\n", encoding="utf-8")
    _console(checkout, "/git_stash root={repo} action=push include_untracked=false", "git_stash")
    assert _hello(checkout) == "hello\n"
    _console(checkout, "/git_stash root={repo} action=pop", "git_stash")
    assert _hello(checkout) == "edited\n"
    git(checkout.repo, "checkout", "-q", "--", "hello.txt")
    # merge
    _console(checkout, "/git_merge root={repo} branch=plain no_ff=false", "git_merge")
    assert _hello(checkout) == "changed\n"


def test_an_attended_apply_patch_works_in_the_live_checkout(checkout):
    from sonder_runtime.adapters import operator_attendance as attendance  # new module

    root = str(checkout.repo)
    patch = _patch("hello.txt", "hello", "hi")
    _refused(harness_tools.apply_patch(root, patch))
    with attendance.attended("apply_patch"):
        assert harness_tools.apply_patch(root, patch)["ok"] is True
        # The attended call still gets the direct check on every path the patch names.
        relative = checkout.shipped.relative_to(checkout.repo).as_posix()
        first = checkout.shipped.read_text(encoding="utf-8").splitlines()[0]
        _refused(harness_tools.apply_patch(root, _patch(relative, first, "{}")))
    assert _hello(checkout).startswith("hi")


def test_an_unattended_console_is_refused_like_any_other_caller(checkout, unattended_console):
    """No person at the console (a piped script, a service): the tool runs as an unattended caller."""
    plain_branch(checkout)
    head = _head(checkout)
    result = _console(checkout, "/git_checkout root={repo} ref=plain", "git_checkout")
    assert "protected" in result, result
    assert _head(checkout) == head and _branch(checkout) == "master"


def test_attendance_is_bound_to_the_tool_that_was_typed(checkout):
    """A person typing ``/agent`` does not attend the git calls the model makes in that turn."""
    from sonder_runtime.adapters import operator_attendance as attendance  # new module

    plain = plain_branch(checkout)
    root = str(checkout.repo)
    with attendance.attended("agent"):
        assert attendance.attended_for("agent") is True
        assert attendance.attended_for("git_checkout") is False
        _refused(harness_tools.git_checkout(root, "plain"))
    assert _branch(checkout) == "master"
    assert attendance.attended_for("git_checkout") is False
    with attendance.attended("git_checkout"):
        assert harness_tools.git_checkout(root, "plain")["ok"] is True
        # Still not attended for a different tool, even inside this block.
        _refused(harness_tools.git_merge(root, "master"))
    assert _head(checkout) == plain
    with attendance.attended("git_checkout"), attendance.unattended():
        assert attendance.attended_for("git_checkout") is False


# ---- everything that is not the live checkout is unchanged -------------------------------

def _user_repo(tmp_path, name="user_repo"):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "test@test.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "checkout", "-q", "-b", "plain")
    (repo / "hello.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "plain change")
    git(repo, "checkout", "-q", "master")
    return repo


def test_a_users_repository_keeps_every_git_tool(policy_files, tmp_path, request):
    repo = _user_repo(tmp_path)
    root = str(repo)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    plain = git(repo, "rev-parse", "plain").stdout.strip()
    # a branch named like a commit id, then a checkout by that id: ordinary git, allowed here
    git(repo, "branch", head, plain)
    assert harness_tools.git_checkout(root, "plain")["ok"] is True
    assert harness_tools.git_checkout(root, "master")["ok"] is True
    assert harness_tools.git_branch(root, "mine", base="plain")["ok"] is True
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == plain
    assert harness_tools.git_checkout(root, "master")["ok"] is True
    assert harness_tools.git_merge(root, "plain")["ok"] is True
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "changed\n"
    git(repo, "reset", "-q", "--hard", head)
    assert harness_tools.git_cherry_pick(root, json.dumps([plain]))["ok"] is True
    (repo / "hello.txt").write_text("edited\n", encoding="utf-8")
    assert harness_tools.git_stash(root, action="push", include_untracked=False)["ok"] is True
    assert harness_tools.git_stash(root, action="pop")["ok"] is True
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "edited\n"
    no_child = request.getfixturevalue("no_child")  # from here on, formatters are recorded, not run
    assert harness_tools.format_code(root, tool="ruff", path="")["ok"] is True
    assert len(no_child) == 1


def test_a_git_worktree_of_the_checkout_is_not_the_live_checkout(checkout, tmp_path):
    """A worktree has its own files (its ``.git`` is a file): changing them never changes the runtime."""
    plain = plain_branch(checkout)
    worktree = tmp_path / "wt"
    git(checkout.repo, "worktree", "add", "-q", "--detach", str(worktree), "master")
    result = harness_tools.git_checkout(str(worktree), "plain")
    assert result["ok"] is True, result
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == plain
    assert _head(checkout) != plain  # the live checkout was not touched


@windows_only
def test_the_live_checkout_is_found_through_a_junction_and_a_different_case(checkout, tmp_path):
    plain_branch(checkout)
    alias = tmp_path / "alias"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(checkout.repo)],
                          capture_output=True, check=False)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    head = _head(checkout)
    try:
        for spelled in (str(alias), str(alias).upper(), str(checkout.repo).upper()):
            _refused(harness_tools.git_checkout(spelled, "plain"))
            assert _head(checkout) == head
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(alias)], capture_output=True, check=False)


def test_apply_patch_still_checks_its_paths_outside_the_live_checkout(policy_files, tmp_path, monkeypatch):
    """The direct protected-path check on a patch is not limited to the live checkout."""
    repo = _user_repo(tmp_path)
    db = repo / "memory.db"
    db.write_text("one\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "db")
    monkeypatch.setenv("SONDER_DB", str(db))
    result = harness_tools.apply_patch(str(repo), _patch("memory.db", "one", "two"))
    assert result["ok"] is False and "protected" in str(result.get("error", "")), result
    assert db.read_text(encoding="utf-8") == "one\n"
    # Control: an ordinary file in the same repository is patched.
    result = harness_tools.apply_patch(str(repo), _patch("hello.txt", "hello", "hi"))
    assert result["ok"] is True, result
    assert (repo / "hello.txt").read_text(encoding="utf-8").startswith("hi")
