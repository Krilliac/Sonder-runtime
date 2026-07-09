"""Safely update a Trilobite Git checkout while preserving local edits."""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run(args, cwd):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    out = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    return proc.returncode, out


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Git checkout to update")
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

    stashed = False
    if status.strip():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        print("[trilobite] saving local edits before update...")
        code, out = run(
            [
                "stash",
                "push",
                "--include-untracked",
                "-m",
                "trilobite gui update backup %s" % stamp,
            ],
            repo,
        )
        print(out)
        if code != 0:
            print("ERROR: could not save local edits. Commit or move them, then retry.")
            return 1
        stashed = True

    print("[trilobite] fetching latest main...")
    code, out = run(["fetch", "origin", "main"], repo)
    print(out)
    if code != 0:
        if stashed:
            print("Your local edits are saved in git stash. Run: git stash list")
        return 1

    print("[trilobite] rebasing local checkout...")
    code, out = run(["rebase", "origin/main"], repo)
    print(out)
    if code != 0:
        print("ERROR: update failed. If needed, run: git rebase --abort")
        if stashed:
            print("Your local edits are saved in git stash. Run: git stash list")
        return 1

    if stashed:
        print("[trilobite] restoring saved local edits...")
        code, out = run(["stash", "apply"], repo)
        print(out)
        if code != 0:
            print(
                "WARNING: updated to latest main, but saved local edits need "
                "manual conflict resolution."
            )
            print("Your backup stash was kept. Run: git stash list")
            return 2
        run(["stash", "drop"], repo)

    print("[trilobite] update complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
