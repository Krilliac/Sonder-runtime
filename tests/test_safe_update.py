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
