"""Safely update a Sonder Git checkout while preserving local edits."""

import argparse
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path


# Every git call is bounded: a hung credential prompt, network stall, or lock
# wait must fail the update instead of blocking the GUI's updater forever.
GIT_TIMEOUT_SECONDS = 120
GIT_NETWORK_TIMEOUT_SECONDS = 300
TIMEOUT_EXIT_CODE = 124


def run(args, cwd):
    timeout = (
        GIT_NETWORK_TIMEOUT_SECONDS if args and args[0] == "fetch"
        else GIT_TIMEOUT_SECONDS
    )
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TIMEOUT_EXIT_CODE, "git %s timed out after %ss" % (
            args[0] if args else "", timeout,
        )
    out = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    return proc.returncode, out


def _stash_entries(repo):
    """Return [(sha, subject)] for the current stash stack, newest first.

    The stash stack is shared across every worktree of a repository, and any
    concurrent process may push or pop entries while an update runs.  Every
    stash operation below therefore addresses our own entry by its commit SHA,
    never by the positional ``stash@{0}`` that ``apply``/``drop`` default to.
    """
    code, out = run(["stash", "list", "--format=%H %gs"], repo)
    if code != 0:
        return None
    entries = []
    for line in out.splitlines():
        sha, _, subject = line.strip().partition(" ")
        if sha:
            entries.append((sha, subject))
    return entries


def _find_stash_sha(repo, tag):
    entries = _stash_entries(repo)
    if not entries:
        return None
    for sha, subject in entries:
        if tag in subject:
            return sha
    return None


def _stash_ref_for_sha(repo, sha):
    """Current ``stash@{n}`` for a stash commit, or None if it is gone."""
    code, out = run(["stash", "list", "--format=%H"], repo)
    if code != 0:
        return None
    for index, line in enumerate(out.splitlines()):
        if line.strip() == sha:
            return "stash@{%d}" % index
    return None


def _restore_after_failed_rebase(repo, pre_head, pre_branch, stash_sha):
    """Abort a failed rebase and put the checkout back where it started.

    Returns True only when HEAD (and the branch, if one was checked out) is
    back at the pre-update commit with no rebase in progress, and the saved
    local edits were re-applied cleanly (their stash entry is then dropped).
    """
    code, out = run(["rebase", "--abort"], repo)
    if out:
        print(out)
    code, head = run(["rev-parse", "HEAD"], repo)
    if code != 0 or head.strip() != pre_head:
        print(
            "ERROR: could not restore the checkout to %s after the failed "
            "rebase. Run: git status" % pre_head
        )
        return False
    if pre_branch:
        code, branch = run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
        if code != 0 or branch.strip() != pre_branch:
            print(
                "ERROR: checkout is at %s but not on branch %s. Run: git status"
                % (pre_head, pre_branch)
            )
            return False
    if stash_sha:
        code, out = run(["stash", "apply", stash_sha], repo)
        if out:
            print(out)
        if code != 0:
            print(
                "ERROR: checkout restored to %s, but saved local edits could "
                "not be re-applied cleanly. Your backup stash was kept. Run: "
                "git stash list" % pre_head
            )
            return False
        ref = _stash_ref_for_sha(repo, stash_sha)
        if ref is not None:
            run(["stash", "drop", ref], repo)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Git checkout to update")
    parser.add_argument(
        "--keep-failed-rebase",
        action="store_true",
        help=(
            "leave a conflicted rebase in progress for manual resolution "
            "instead of aborting it and restoring the previous revision"
        ),
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    code, out = run(["rev-parse", "--is-inside-work-tree"], repo)
    if code != 0:
        print("ERROR: %s is not a Git checkout.\n%s" % (repo, out))
        return 1

    code, status = run(["status", "--porcelain"], repo)
    if code != 0:
        print("ERROR: could not inspect local changes.\n%s" % status)
        return 1

    code, pre_head = run(["rev-parse", "HEAD"], repo)
    if code != 0:
        print("ERROR: could not resolve the current revision.\n%s" % pre_head)
        return 1
    pre_head = pre_head.strip()
    code, pre_branch = run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    pre_branch = pre_branch.strip() if code == 0 else ""

    stash_sha = None
    if status.strip():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # The tag makes this entry findable by content even if other processes
        # push their own stashes concurrently; the SHA pins the exact entry so
        # apply/drop can never touch someone else's stash.
        tag = "sonder-update-%s-%s" % (stamp, uuid.uuid4().hex[:12])
        print("[sonder] saving local edits before update...")
        code, out = run(
            [
                "stash",
                "push",
                "--include-untracked",
                "-m",
                "sonder gui update backup %s" % tag,
            ],
            repo,
        )
        print(out)
        if code != 0:
            print("ERROR: could not save local edits. Commit or move them, then retry.")
            return 1
        stash_sha = _find_stash_sha(repo, tag)
        if stash_sha is None:
            print(
                "ERROR: saved local edits but could not identify the stash entry. "
                "Refusing to continue; run: git stash list"
            )
            return 1

    print("[sonder] fetching latest main...")
    code, out = run(["fetch", "origin", "main"], repo)
    print(out)
    if code != 0:
        if stash_sha:
            print("Your local edits are saved in git stash. Run: git stash list")
        return 1

    print("[sonder] rebasing local checkout...")
    code, out = run(["rebase", "origin/main"], repo)
    print(out)
    if code != 0:
        if args.keep_failed_rebase:
            print(
                "ERROR: update failed; the rebase was left in progress as "
                "requested. Resolve it, or run: git rebase --abort"
            )
            if stash_sha:
                print("Your local edits are saved in git stash. Run: git stash list")
            return 1
        if _restore_after_failed_rebase(repo, pre_head, pre_branch, stash_sha):
            print("ERROR: update aborted; checkout restored to %s" % pre_head)
        return 1

    if stash_sha:
        print("[sonder] restoring saved local edits...")
        code, out = run(["stash", "apply", stash_sha], repo)
        print(out)
        if code != 0:
            print(
                "WARNING: updated to latest main, but saved local edits need "
                "manual conflict resolution."
            )
            print("Your backup stash was kept. Run: git stash list")
            return 2
        # Drop exactly our entry.  Its position may have shifted if another
        # process pushed or popped stashes meanwhile, so re-resolve the SHA to
        # its current stash@{n}; if it is already gone, there is nothing to do.
        ref = _stash_ref_for_sha(repo, stash_sha)
        if ref is not None:
            run(["stash", "drop", ref], repo)

    print("[sonder] update complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
