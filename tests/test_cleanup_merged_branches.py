import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import cleanup_merged_branches as cleanup


def _run(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(remote, "git", "init", "--bare", "--initial-branch=main")
    repo = tmp_path / "repo"
    _run(tmp_path, "git", "clone", str(remote), str(repo))
    _run(repo, "git", "config", "user.name", "Test User")
    _run(repo, "git", "config", "user.email", "test@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _run(repo, "git", "add", "base.txt")
    _run(repo, "git", "commit", "-m", "base")
    _run(repo, "git", "push", "-u", "origin", "main")
    return repo, remote


def _feature(repo: Path, tmp_path: Path, *, merge: bool = True) -> Path:
    _run(repo, "git", "switch", "-c", "feature/merged")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run(repo, "git", "add", "feature.txt")
    _run(repo, "git", "commit", "-m", "feature")
    _run(repo, "git", "push", "-u", "origin", "feature/merged")
    _run(repo, "git", "switch", "main")
    if merge:
        _run(repo, "git", "merge", "--no-ff", "feature/merged", "-m", "merge feature")
        _run(repo, "git", "push", "origin", "main")
    worktree = tmp_path / "feature-worktree"
    _run(repo, "git", "worktree", "add", str(worktree), "feature/merged")
    return worktree


def test_dry_run_discovers_merged_branch_without_mutation(tmp_path, capsys):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)

    assert cleanup.main([
        "--repo", str(repo), "--branch", "feature/merged", "--json"
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    candidate = report["candidates"][0]
    assert report["applied"] is False
    assert candidate["eligible"] is True
    assert candidate["evidence"]["local_tip_in_remote_main"] is True
    assert candidate["evidence"]["remote_tip_in_remote_main"] is True
    assert Path(candidate["worktree"]) == worktree.resolve()
    assert worktree.exists()
    assert _run(repo, "git", "branch", "--list", "feature/merged")
    assert _run(remote, "git", "branch", "--list", "feature/merged")


def test_apply_deletes_remote_then_clean_worktree_and_local_branch(tmp_path, capsys):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)

    assert cleanup.main([
        "--repo", str(repo), "--branch", "feature/merged", "--apply", "--json"
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["results"] == [{
        "actions": [
            "remote_branch_deleted", "worktree_removed", "local_branch_deleted"
        ],
        "branch": "feature/merged",
        "status": "deleted",
    }]
    assert not worktree.exists()
    assert not _run(repo, "git", "branch", "--list", "feature/merged")
    assert not _run(remote, "git", "branch", "--list", "feature/merged")


def test_dirty_worktree_is_refused_without_any_deletion(tmp_path, capsys):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    (worktree / "untracked.txt").write_text("do not lose\n", encoding="utf-8")

    assert cleanup.main([
        "--repo", str(repo), "--branch", "feature/merged", "--apply", "--json"
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"][0]["reasons"] == ["DIRTY_WORKTREE"]
    assert report["results"] == [
        {"branch": "feature/merged", "status": "refused"}
    ]
    assert worktree.exists()
    assert _run(repo, "git", "branch", "--list", "feature/merged")
    assert _run(remote, "git", "branch", "--list", "feature/merged")


def test_ignored_only_worktree_content_is_refused_without_deletion(tmp_path, capsys):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.env\n", encoding="utf-8")
    (worktree / ".env").write_text("TOKEN=do-not-delete\n", encoding="utf-8")

    assert cleanup.main([
        "--repo", str(repo), "--branch", "feature/merged", "--apply", "--json"
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"][0]["reasons"] == ["DIRTY_WORKTREE"]
    assert report["results"] == [
        {"branch": "feature/merged", "status": "refused"}
    ]
    assert (worktree / ".env").read_text(encoding="utf-8") == "TOKEN=do-not-delete\n"
    assert _run(repo, "git", "branch", "--list", "feature/merged")
    assert _run(remote, "git", "branch", "--list", "feature/merged")


def test_locked_worktree_is_refused(tmp_path):
    repo, _ = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    _run(repo, "git", "worktree", "lock", "--reason", "active", str(worktree))
    candidate = cleanup.audit(repo, selected=["feature/merged"])[0]
    assert candidate.eligible is False
    assert "LOCKED_WORKTREE" in candidate.reasons


def test_unmerged_local_and_remote_tips_are_refused(tmp_path):
    repo, _ = _repo(tmp_path)
    _feature(repo, tmp_path, merge=False)
    candidate = cleanup.audit(repo, selected=["feature/merged"])[0]
    assert candidate.eligible is False
    assert "LOCAL_TIP_NOT_MERGED" in candidate.reasons
    assert "REMOTE_TIP_NOT_MERGED" in candidate.reasons


def test_apply_refreshes_and_refuses_a_new_unmerged_remote_tip(tmp_path, capsys):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    other = tmp_path / "other"
    _run(tmp_path, "git", "clone", str(remote), str(other))
    _run(other, "git", "config", "user.name", "Other User")
    _run(other, "git", "config", "user.email", "other@example.invalid")
    _run(other, "git", "switch", "feature/merged")
    (other / "remote-only.txt").write_text("not merged\n", encoding="utf-8")
    _run(other, "git", "add", "remote-only.txt")
    _run(other, "git", "commit", "-m", "remote branch advanced")
    _run(other, "git", "push", "origin", "feature/merged")

    assert cleanup.main([
        "--repo", str(repo), "--branch", "feature/merged", "--apply", "--json"
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"][0]["reasons"] == ["REMOTE_TIP_NOT_MERGED"]
    assert worktree.exists()
    assert _run(remote, "git", "branch", "--list", "feature/merged")


def test_remote_delete_lease_refuses_tip_advanced_after_audit(tmp_path):
    repo, remote = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    candidate = cleanup.audit(repo, selected=["feature/merged"])[0]
    assert candidate.eligible is True
    inspected_tip = candidate.remote_tip

    other = tmp_path / "other-after-audit"
    _run(tmp_path, "git", "clone", str(remote), str(other))
    _run(other, "git", "config", "user.name", "Other User")
    _run(other, "git", "config", "user.email", "other@example.invalid")
    _run(other, "git", "switch", "feature/merged")
    (other / "advanced.txt").write_text("advanced after audit\n", encoding="utf-8")
    _run(other, "git", "add", "advanced.txt")
    _run(other, "git", "commit", "-m", "advance after audit")
    advanced_tip = _run(other, "git", "rev-parse", "HEAD")
    _run(other, "git", "push", "origin", "feature/merged")
    assert advanced_tip != inspected_tip

    results = cleanup.cleanup(repo, [candidate], remote="origin")
    assert results == [{
        "branch": "feature/merged", "status": "failed_remote_delete",
    }]
    assert _run(remote, "git", "rev-parse", "refs/heads/feature/merged") == advanced_tip
    assert worktree.exists()
    assert _run(repo, "git", "branch", "--list", "feature/merged")


def test_refused_missing_worktree_registration_is_not_globally_pruned(tmp_path):
    repo, _ = _repo(tmp_path)
    worktree = _feature(repo, tmp_path)
    shutil.rmtree(worktree)
    before = _run(repo, "git", "worktree", "list", "--porcelain")
    assert worktree.as_posix() in before
    candidate = cleanup.audit(repo, selected=["feature/merged"])[0]
    assert "MISSING_WORKTREE" in candidate.reasons

    assert cleanup.cleanup(repo, [candidate], remote="origin") == [
        {"branch": "feature/merged", "status": "refused"}
    ]
    after = _run(repo, "git", "worktree", "list", "--porcelain")
    assert worktree.as_posix() in after


def test_current_and_protected_branch_are_refused(tmp_path):
    repo, _ = _repo(tmp_path)
    candidate = cleanup.audit(repo, selected=["main"])[0]
    assert candidate.eligible is False
    assert {"CURRENT_WORKTREE", "PROTECTED_BRANCH"} <= set(candidate.reasons)


def test_exact_repository_root_is_required(tmp_path):
    repo, _ = _repo(tmp_path)
    child = repo / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="exact resolved"):
        cleanup.resolve_repo(child)


def test_remote_option_injection_is_rejected(tmp_path):
    repo, _ = _repo(tmp_path)
    with pytest.raises(ValueError, match="remote name"):
        cleanup.audit(repo, remote="--upload-pack=malicious")


def test_apply_requires_an_explicit_branch(tmp_path):
    repo, _ = _repo(tmp_path)
    with pytest.raises(SystemExit):
        cleanup.main(["--repo", str(repo), "--apply"])


def test_report_is_deterministically_sorted(tmp_path):
    repo, _ = _repo(tmp_path)
    first = cleanup.Candidate("z-last", "a" * 40, None, None)
    second = cleanup.Candidate("a-first", "b" * 40, None, None)
    report = cleanup.build_report(repo, [first, second], applied=False)
    assert [item["branch"] for item in report["candidates"]] == [
        "a-first", "z-last"
    ]
