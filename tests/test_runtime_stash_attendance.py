"""``runtime_source_stash pop`` writes into the live runtime checkout: the operator, and only its own stash.

``pop`` restores old edits into the checkout Sonder runs from, the same class
of change the live-checkout rule keeps from unattended callers, so it runs
only when the operator typed the tool at the console
(``operator_attendance``, bound to ``runtime_source_stash``): through
``/runtime_source_stash action=pop`` or the console command ``/stash pop``.

The stash list is shared by every worktree of a repository and a stash
message is free text, so a model could push a "sonder runtime recovery" stash
from a worktree of the runtime (``git_stash`` is allowed there) and have the
operator restore it.  ``save`` therefore records the commit id of each stash it
makes in the checkout's own git directory, and ``pop`` restores only a
recorded one, by commit id.
"""
from __future__ import annotations

import subprocess

import pytest

import git_tools
import harness_tools
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.adapters import operator_attendance

TOOL = "runtime_source_stash"


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


@pytest.fixture
def runtime_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Sonder Tests")
    _git(repo, "config", "user.email", "sonder-tests@example.invalid")
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "remote", "add", "origin", "https://github.com/Krilliac/Sonder-runtime.git")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    return repo


def _tracked(repo):
    return (repo / "tracked.txt").read_text(encoding="utf-8")


def _save(repo, text):
    (repo / "tracked.txt").write_text(text, encoding="utf-8")
    assert git_tools.runtime_stash(repo, "save")["changed"] is True
    assert _tracked(repo) == "first\n"


def _attended_pop(repo):
    with operator_attendance.attended(TOOL):
        return git_tools.runtime_stash(repo, "pop")


# ---- attendance ---------------------------------------------------------------------------

def test_an_unattended_pop_is_refused(runtime_repo):
    _save(runtime_repo, "recovery\n")
    with pytest.raises(PermissionError, match="console"):
        git_tools.runtime_stash(runtime_repo, "pop")
    # Not attended for another tool either (an operator who typed /agent, say).
    with operator_attendance.attended("agent"), pytest.raises(PermissionError, match="console"):
        git_tools.runtime_stash(runtime_repo, "pop")
    assert _tracked(runtime_repo) == "first\n"
    assert git_tools.runtime_stash_status(runtime_repo)["stash_count"] == 1


def test_the_mcp_tool_reports_an_unattended_pop_as_refused(runtime_repo, monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    monkeypatch.setattr(server, "_runtime_source_root", lambda: runtime_repo)
    _save(runtime_repo, "recovery\n")
    out = server.runtime_source_stash("pop")
    assert out.startswith("runtime source stash refused") and "console" in out, out
    assert _tracked(runtime_repo) == "first\n"


def test_an_attended_pop_restores_the_recovery_stash(runtime_repo):
    _save(runtime_repo, "recovery\n")
    restored = _attended_pop(runtime_repo)
    assert restored["changed"] is True
    assert _tracked(runtime_repo) == "recovery\n"
    assert restored["after"]["stash_count"] == 0
    assert "sonder runtime recovery" not in _git(runtime_repo, "stash", "list")


def test_the_console_tool_dispatch_attends_the_pop(runtime_repo, monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    monkeypatch.setattr(server, "_runtime_source_root", lambda: runtime_repo)
    monkeypatch.setattr(sonder_repl, "_permission_gate", lambda tool, arguments=None: (True, ""))
    _save(runtime_repo, "recovery\n")
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)  # a piped console
    out = sonder_repl._run_catalogued("/runtime_source_stash action=pop", "/runtime_source_stash")
    assert "refused" in out and _tracked(runtime_repo) == "first\n", out
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)  # a person at the console
    out = sonder_repl._run_catalogued("/runtime_source_stash action=pop", "/runtime_source_stash")
    assert _tracked(runtime_repo) == "recovery\n", out


@pytest.mark.parametrize("command", ["/stash", "/runtime-stash"])
def test_the_stash_console_command_attends_only_its_tool_and_only_with_a_person_present(monkeypatch, command):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    with sonder_repl._console_attendance(command):
        assert operator_attendance.attended_for(TOOL) is True
        assert operator_attendance.attended_for("git_checkout") is False
    assert operator_attendance.attended_for(TOOL) is False
    with sonder_repl._console_attendance("/update"):
        assert operator_attendance.attended_for(TOOL) is False
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    with sonder_repl._console_attendance(command):
        assert operator_attendance.attended_for(TOOL) is False


# ---- only a stash this checkout's save recorded --------------------------------------------

def _plant_from_a_worktree(repo, tmp_path, text):
    """A model's route: ``git_stash push`` in a worktree of the runtime, with the recovery message copied."""
    worktree = tmp_path / "wt"
    if not worktree.exists():
        _git(repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    (worktree / "tracked.txt").write_text(text, encoding="utf-8")
    result = harness_tools.git_stash(str(worktree), action="push", message="sonder runtime recovery",
                                     include_untracked=False)
    assert result["ok"] is True, result


def test_a_stash_planted_from_another_worktree_is_not_popped(runtime_repo, tmp_path):
    _plant_from_a_worktree(runtime_repo, tmp_path, "planted\n")
    assert "sonder runtime recovery" in _git(runtime_repo, "stash", "list")  # shared with the live checkout
    assert git_tools.runtime_stash_status(runtime_repo)["stash_count"] == 0
    with pytest.raises(ValueError, match="saved by runtime_source_stash"):
        _attended_pop(runtime_repo)
    assert _tracked(runtime_repo) == "first\n"


def test_a_copied_message_in_the_same_checkout_is_not_popped(runtime_repo):
    """Any push the ledger did not record, even in this checkout, is not a recovery stash."""
    (runtime_repo / "tracked.txt").write_text("copied\n", encoding="utf-8")
    _git(runtime_repo, "stash", "push", "--message", "sonder runtime recovery")
    with pytest.raises(ValueError, match="saved by runtime_source_stash"):
        _attended_pop(runtime_repo)
    assert _tracked(runtime_repo) == "first\n"


def _stash_ids(repo):
    return _git(repo, "stash", "list", "--format=%H").split()


def test_a_newer_planted_stash_does_not_displace_the_recorded_one(runtime_repo, tmp_path):
    _save(runtime_repo, "recovery\n")
    _plant_from_a_worktree(runtime_repo, tmp_path, "planted\n")  # newest entry: stash@{0}
    planted = _stash_ids(runtime_repo)[0]
    assert git_tools.runtime_stash_status(runtime_repo)["top"] == "stash@{1}"
    _attended_pop(runtime_repo)
    assert _tracked(runtime_repo) == "recovery\n"
    assert _stash_ids(runtime_repo) == [planted]  # the planted one is left alone, not restored or dropped


# ---- a pop whose drop did not complete ------------------------------------------------------

def _ledger(repo):
    return (repo / ".git" / git_tools.RUNTIME_STASH_LEDGER).read_text(encoding="ascii").split("\n")


def test_a_failed_drop_leaves_the_stash_applied_once(runtime_repo, monkeypatch):
    """Apply succeeded, drop failed: a retry reports it instead of applying the same stash again."""
    _save(runtime_repo, "recovery\n")
    recorded = _stash_ids(runtime_repo)[0]
    real = git_tools._checked_git

    def failing_drop(root, arguments, **kwargs):
        if kwargs.get("operation") == "stash drop":
            raise ValueError("git stash drop failed (exit 1): simulated")
        return real(root, arguments, **kwargs)

    monkeypatch.setattr(git_tools, "_checked_git", failing_drop)
    with pytest.raises(ValueError, match="simulated"):
        _attended_pop(runtime_repo)
    monkeypatch.setattr(git_tools, "_checked_git", real)
    assert _tracked(runtime_repo) == "recovery\n"
    assert _stash_ids(runtime_repo) == [recorded]
    assert "applied " + recorded in _ledger(runtime_repo)
    _git(runtime_repo, "checkout", "--", "tracked.txt")  # the operator undoes it; the checkout is clean again
    with pytest.raises(git_tools.RecoveryStashAlreadyApplied, match="already applied") as raised:
        _attended_pop(runtime_repo)
    assert raised.value.commit == recorded and raised.value.reference == "stash@{0}"
    assert _tracked(runtime_repo) == "first\n"  # not applied a second time
    assert _stash_ids(runtime_repo) == [recorded]


def test_a_drop_that_removes_another_entry_stores_it_back(runtime_repo, tmp_path, monkeypatch):
    """A stash pushed from another worktree between the list and the drop shifts ``stash@{n}``."""
    _save(runtime_repo, "recovery\n")
    recorded = _stash_ids(runtime_repo)[0]
    worktree = tmp_path / "wt"
    _git(runtime_repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    real = git_tools._checked_git

    def push_before_drop(root, arguments, **kwargs):
        if kwargs.get("operation") == "stash drop":
            (worktree / "tracked.txt").write_text("concurrent\n", encoding="utf-8")
            _git(worktree, "stash", "push", "-q", "--message", "concurrent")
        return real(root, arguments, **kwargs)

    monkeypatch.setattr(git_tools, "_checked_git", push_before_drop)
    with pytest.raises(git_tools.RecoveryStashDropMismatch, match="stored back") as raised:
        _attended_pop(runtime_repo)
    monkeypatch.setattr(git_tools, "_checked_git", real)
    concurrent = raised.value.dropped
    assert raised.value.commit == recorded and raised.value.restored is True and concurrent != recorded
    assert sorted(_stash_ids(runtime_repo)) == sorted([recorded, concurrent])  # nothing lost
    assert "concurrent" in _git(runtime_repo, "stash", "list", "--format=%gs")
    assert _tracked(runtime_repo) == "recovery\n"
    assert "applied " + recorded in _ledger(runtime_repo)


# ---- git_stash does not take the recovery stash ---------------------------------------------

def _refused_as_recovery(result):
    assert result.get("ok") is False and result.get("guard") == harness_tools.RECOVERY_STASH_GUARD, result
    assert "runtime_source_stash" in result["error"]


@pytest.mark.parametrize("action", ["pop", "drop"])
def test_git_stash_does_not_take_the_recorded_recovery_stash(runtime_repo, tmp_path, action):
    _save(runtime_repo, "recovery\n")
    recorded = _stash_ids(runtime_repo)
    # In the checkout itself, even for an operator who typed git_stash at the console.
    with operator_attendance.attended("git_stash"):
        _refused_as_recovery(harness_tools.git_stash(str(runtime_repo), action=action))
    # And from another worktree, which shares the stash list.
    worktree = tmp_path / "wt"
    _git(runtime_repo, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    _refused_as_recovery(harness_tools.git_stash(str(worktree), action=action))
    assert _stash_ids(runtime_repo) == recorded
    assert _tracked(runtime_repo) == "first\n" and _tracked(worktree) == "first\n"


def test_git_stash_still_takes_an_unrecorded_entry_above_the_recovery_stash(runtime_repo, tmp_path):
    _save(runtime_repo, "recovery\n")
    recorded = _stash_ids(runtime_repo)
    _plant_from_a_worktree(runtime_repo, tmp_path, "planted\n")
    assert harness_tools.git_stash(str(tmp_path / "wt"), action="drop")["ok"] is True
    assert _stash_ids(runtime_repo) == recorded
    _refused_as_recovery(harness_tools.git_stash(str(tmp_path / "wt"), action="drop"))


def test_a_ledger_in_a_linked_worktree_protects_its_stash_from_the_main_checkout(runtime_repo, tmp_path):
    """The runtime checkout may itself be a linked worktree: its ledger is under ``worktrees/<name>``."""
    _git(runtime_repo, "checkout", "-q", "-b", "elsewhere")
    runtime = tmp_path / "runtime-wt"
    _git(runtime_repo, "worktree", "add", "-q", str(runtime), "main")
    _save(runtime, "recovery\n")
    assert list((runtime_repo / ".git" / "worktrees").glob("*/" + git_tools.RUNTIME_STASH_LEDGER))
    _refused_as_recovery(harness_tools.git_stash(str(runtime_repo), action="drop"))
    assert len(_stash_ids(runtime_repo)) == 1


def test_stash_pop_typed_at_the_console_restores(runtime_repo, monkeypatch):
    """The console's ``/stash pop`` path: attendance from the REPL, then the control chain with the operator's yes."""
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    monkeypatch.setattr(server, "_runtime_source_root", lambda: runtime_repo)
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    _save(runtime_repo, "recovery\n")
    out = server.control_command("/stash pop", operator_approved=True)  # no attendance: refused
    assert "refused" in out and _tracked(runtime_repo) == "first\n", out
    with sonder_repl._console_attendance("/stash"):
        out = server.control_command("/stash pop", operator_approved=True)
    assert _tracked(runtime_repo) == "recovery\n", out
