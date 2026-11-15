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


def test_a_newer_planted_stash_does_not_displace_the_recorded_one(runtime_repo, tmp_path):
    _save(runtime_repo, "recovery\n")
    _plant_from_a_worktree(runtime_repo, tmp_path, "planted\n")  # newest entry: stash@{0}
    assert git_tools.runtime_stash_status(runtime_repo)["top"] == "stash@{1}"
    _attended_pop(runtime_repo)
    assert _tracked(runtime_repo) == "recovery\n"
    listed = _git(runtime_repo, "stash", "list")
    assert listed.count("sonder runtime recovery") == 1  # the planted one is left alone, not restored


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
