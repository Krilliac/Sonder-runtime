import shutil
import subprocess

import safe_update


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=True,
    )


def test_safe_update_preserves_local_edits(tmp_path):
    if shutil.which("git") is None:
        return
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    clone = tmp_path / "clone"

    git(tmp_path, "init", "--bare", "--initial-branch=main", origin.name)
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    (work / "README.md").write_text("one\n", encoding="utf-8")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "one")
    git(work, "push", "origin", "main")

    git(tmp_path, "clone", str(origin), str(clone))
    (clone / "local.txt").write_text("keep me\n", encoding="utf-8")

    (work / "README.md").write_text("two\n", encoding="utf-8")
    git(work, "commit", "-am", "two")
    git(work, "push", "origin", "main")

    assert safe_update.main(["--repo", str(clone)]) == 0
    assert (clone / "README.md").read_text(encoding="utf-8") == "two\n"
    assert (clone / "local.txt").read_text(encoding="utf-8") == "keep me\n"


def _seed_repos(tmp_path):
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    clone = tmp_path / "clone"

    git(tmp_path, "init", "--bare", "--initial-branch=main", origin.name)
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test User")
    (work / "README.md").write_text("one\n", encoding="utf-8")
    git(work, "add", "README.md")
    git(work, "commit", "-m", "one")
    git(work, "push", "origin", "main")

    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "user.name", "Test User")

    (work / "README.md").write_text("two\n", encoding="utf-8")
    git(work, "commit", "-am", "two")
    git(work, "push", "origin", "main")
    return clone


def test_concurrent_stash_is_not_applied_or_dropped(tmp_path, monkeypatch):
    """A stash pushed by another process mid-update must never be touched.

    The stash stack is shared across worktrees and sessions.  The historical
    failure mode: safe_update stashed local edits, another process pushed its
    own stash on top, and the bare ``stash apply`` + ``stash drop`` then
    applied and destroyed the *other* process's entry.
    """
    if shutil.which("git") is None:
        return
    clone = _seed_repos(tmp_path)
    (clone / "local.txt").write_text("keep me\n", encoding="utf-8")

    real_run = safe_update.run
    state = {"injected": False}

    def run_with_interleaving(args, cwd):
        result = real_run(args, cwd)
        if (
            not state["injected"]
            and args[0] == "stash"
            and args[1] == "push"
            and any("sonder" in str(part) for part in args)
        ):
            # Simulate a concurrent session stashing its own work immediately
            # after ours, so the foreign entry becomes stash@{0}.
            state["injected"] = True
            (clone / "foreign.txt").write_text("other session\n", encoding="utf-8")
            code, out = real_run(
                ["stash", "push", "--include-untracked", "-m", "other session backup"],
                clone,
            )
            assert code == 0, out
        return result

    monkeypatch.setattr(safe_update, "run", run_with_interleaving)
    assert safe_update.main(["--repo", str(clone)]) == 0

    # Our edits came back; the foreign stash's content did not leak into the
    # worktree, and its entry is still on the stack while ours was dropped.
    assert (clone / "local.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (clone / "foreign.txt").exists()
    code, out = real_run(["stash", "list"], clone)
    assert code == 0
    assert "other session backup" in out
    assert "sonder gui update backup" not in out


def test_unidentifiable_stash_fails_closed_before_rebase(tmp_path, monkeypatch):
    """If the pushed stash cannot be located by tag, stop before mutating."""
    if shutil.which("git") is None:
        return
    clone = _seed_repos(tmp_path)
    (clone / "local.txt").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(safe_update, "_find_stash_sha", lambda repo, tag: None)

    assert safe_update.main(["--repo", str(clone)]) == 1
    # The stash entry itself still exists for manual recovery.
    code, out = git(clone, "stash", "list").returncode, git(clone, "stash", "list").stdout
    assert code == 0
    assert "sonder gui update backup" in out
    # No rebase happened: README still at the old commit.
    assert (clone / "README.md").read_text(encoding="utf-8") == "one\n"
