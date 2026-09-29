"""Temporary-repository contract tests for the resume reality barrier."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from sonder_runtime.adapters.workspace_reality import GitWorkspaceReality
from sonder_runtime.application.ports.workspace_reality import scope_intersects


@pytest.fixture
def local_tmp_path(tmp_path) -> Path:
    return tmp_path


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "src.py").write_text("one\n", encoding="utf-8")
    _git(root, "add", "src.py")
    _git(root, "commit", "-qm", "initial")
    return root


def test_identical_tree_short_circuits_and_non_git_is_unchanged(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality()
    snapshot = reality.capture(str(root))
    assert snapshot and reality.revalidate(str(root), snapshot) is None
    plain = tmp_path / "bare"
    _git(tmp_path, "init", "--bare", str(plain))
    assert reality.capture(str(plain)) is None


def test_dirty_edit_and_owned_scope_require_replan(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality()
    snapshot = reality.capture(str(root))
    (root / "src.py").write_text("two\n", encoding="utf-8")
    delta = reality.revalidate(str(root), snapshot, ("src.py",))
    assert delta and delta["status"] == "changed"
    assert delta["requires_replan"] is True
    assert any(item["path"] == "src.py" for item in delta["files"])


def test_new_commit_detached_head_and_categories(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality()
    snapshot = reality.capture(str(root))
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "docs")
    delta = reality.revalidate(str(root), snapshot, ("src/",))
    assert delta and delta["ahead"] == 1 and delta["files"][0]["category"] == "doc"
    _git(root, "checkout", "-q", "--detach")
    detached = reality.capture(str(root))
    assert detached and detached["branch"] is None


def test_history_rewrite_is_conservative(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality()
    snapshot = reality.capture(str(root))
    _git(root, "checkout", "-q", "--orphan", "rewrite")
    _git(root, "rm", "-qr", "--cached", ".")
    (root / "src.py").write_text("rewritten\n", encoding="utf-8")
    _git(root, "add", "src.py")
    _git(root, "commit", "-qm", "rewrite")
    delta = reality.revalidate(str(root), snapshot, ("src.py",))
    assert delta and delta["history_rewritten"] is True and delta["requires_replan"] is True


def test_unavailable_git_and_old_snapshot_force_reinspection(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality(git="git-that-does-not-exist", timeout_seconds=.1)
    unknown = reality.capture(str(root))
    assert unknown and unknown["complete"] is False
    current = GitWorkspaceReality().capture(str(root))
    old = dict(current)
    old.pop("complete")
    delta = GitWorkspaceReality().revalidate(str(root), old)
    assert delta and delta["status"] == "unavailable" and delta["requires_reinspection"]


def test_scope_supports_directories_globs_and_absolute_paths(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    files = [{"path": "src/module.py", "status": "M"}]
    assert scope_intersects(("src",), files, str(root))
    assert scope_intersects(("**/*.py",), files, str(root))
    assert scope_intersects((str(root / "src"),), files, str(root))
    assert not scope_intersects(("docs",), files, str(root))
    assert scope_intersects((), files, str(root))


def test_output_and_file_lists_are_bounded(local_tmp_path: Path) -> None:
    tmp_path = local_tmp_path
    root = _repo(tmp_path)
    reality = GitWorkspaceReality()
    snapshot = reality.capture(str(root))
    for index in range(510):
        (root / f"untracked-{index}.txt").write_text("x", encoding="utf-8")
    current = reality.capture(str(root))
    assert current and len(current["dirty"]) <= 500 and current["complete"] is False
    delta = reality.revalidate(str(root), snapshot)
    assert delta and len(delta["files"]) <= 400 and delta["truncated"] is True


def test_revalidate_none_snapshot_non_git_is_unchanged(local_tmp_path: Path) -> None:
    plain = local_tmp_path / "plain"; plain.mkdir()
    _git(plain, "init", "--bare")
    assert GitWorkspaceReality().revalidate(str(plain), None) is None


def test_revalidate_none_snapshot_git_is_degraded(local_tmp_path: Path) -> None:
    root = _repo(local_tmp_path)
    delta = GitWorkspaceReality().revalidate(str(root), None)
    assert delta and delta["status"] == "unavailable"


def test_frozen_dirty_snapshot_is_accepted(local_tmp_path: Path) -> None:
    root = _repo(local_tmp_path); reality = GitWorkspaceReality(); snap = reality.capture(str(root))
    snap["dirty"] = tuple(snap["dirty"])
    assert reality.revalidate(str(root), snap) is None


def test_invalid_head_is_degraded(local_tmp_path: Path) -> None:
    root = _repo(local_tmp_path); snap = GitWorkspaceReality().capture(str(root)); snap["head"] = "bad"
    assert GitWorkspaceReality().revalidate(str(root), snap)["status"] == "unavailable"


def test_invalid_digest_is_degraded(local_tmp_path: Path) -> None:
    root = _repo(local_tmp_path); snap = GitWorkspaceReality().capture(str(root)); snap["dirty_hash"] = "bad"
    assert GitWorkspaceReality().revalidate(str(root), snap)["status"] == "unavailable"


def test_timeout_constructor_is_bounded() -> None:
    with pytest.raises(ValueError): GitWorkspaceReality(timeout_seconds=11)
    with pytest.raises(ValueError): GitWorkspaceReality(timeout_seconds=float("nan"))


def test_scope_traversal_is_conservative() -> None:
    assert scope_intersects(("../secret",), [{"path": "src.py"}], ".")


def test_unknown_category_is_source() -> None:
    assert GitWorkspaceReality._category("native.bin") == "source"


def test_manifest_category_is_manifest() -> None:
    assert GitWorkspaceReality._category("requirements.txt") == "manifest"


def test_migration_category_wins() -> None:
    assert GitWorkspaceReality._category("db/migrations/001.sql") == "migration"


def test_absolute_scope_is_case_insensitive(local_tmp_path: Path) -> None:
    assert scope_intersects((str(local_tmp_path / "SRC"),), [{"path": "src/a.py"}], str(local_tmp_path))


def test_missing_snapshot_version_is_degraded(local_tmp_path: Path) -> None:
    root = _repo(local_tmp_path); snap = GitWorkspaceReality().capture(str(root)); snap.pop("version", None)
    assert GitWorkspaceReality().revalidate(str(root), snap)["requires_reinspection"]


def test_detached_complete_identical_and_branch_only_delta(tmp_path):
    root = _repo(tmp_path)
    port = GitWorkspaceReality()
    before = port.capture(str(root))
    _git(root, "checkout", "-q", "--detach")
    current = port.capture(str(root))
    assert current["complete"] and current["branch"] is None
    assert port.revalidate(str(root), current) is None
    assert port.revalidate(str(root), before)["status"] == "changed"


@pytest.mark.parametrize("commit", [False, True])
def test_rename_retains_both_sides_for_scope(tmp_path, commit):
    root = _repo(tmp_path)
    port = GitWorkspaceReality()
    before = port.capture(str(root))
    _git(root, "mv", "src.py", "moved.py")
    if commit:
        _git(root, "commit", "-qm", "rename")
    delta = port.revalidate(str(root), before, ("src.py",))
    assert {item["path"] for item in delta["files"]} == {"src.py", "moved.py"}
    assert delta["requires_replan"]


def test_nested_root_metadata_and_unavailable_git(tmp_path):
    root = _repo(tmp_path)
    nested = root / "src"
    nested.mkdir()
    target = nested / "new.py"
    target.write_text("payload", encoding="utf-8")
    port = GitWorkspaceReality()
    before = port.capture(str(nested))
    assert before["dirty"][0]["size"] == len("payload")
    target.write_text("changed", encoding="utf-8")
    assert port.revalidate(str(nested), before, ("new.py",))["requires_replan"]
    assert GitWorkspaceReality(git="missing-git").revalidate(str(nested), before)["requires_reinspection"]


def test_shared_deadline_and_real_subprocess_timeout(tmp_path, monkeypatch):
    import sys
    from sonder_runtime.adapters.workspace_reality import _GitFailure
    import sonder_runtime.adapters.workspace_reality as module
    root = _repo(tmp_path)
    port = GitWorkspaceReality(timeout_seconds=0.05)
    before = GitWorkspaceReality().capture(str(root))
    assert port.revalidate(str(root), before, deadline_monotonic=time.monotonic()-1)["status"] == "unavailable"
    original = subprocess.Popen
    def sleeper(*args, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", sleeper)
    started = time.monotonic()
    with pytest.raises(_GitFailure, match="timeout"):
        port._run(root, ["status"], started + .05)
    assert time.monotonic() - started < 2


def test_truncated_committed_files_require_plan_even_outside_visible_scope(tmp_path):
    root = _repo(tmp_path)
    port = GitWorkspaceReality()
    before = port.capture(str(root))
    for i in range(401):
        (root / f"file{i:03}.txt").write_text("x", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "many")
    delta = port.revalidate(str(root), before, ("file400.txt",))
    assert len(delta["files"]) == 400
    assert delta["truncated"] and delta["requires_replan"]


def test_missing_historical_commit_requires_replan(tmp_path):
    root = _repo(tmp_path)
    port = GitWorkspaceReality()
    before = port.capture(str(root))
    before["head"] = "1" * 40
    delta = port.revalidate(str(root), before)
    assert delta["history_rewritten"] and delta["requires_replan"]


def test_staging_dirty_file_changes_identity_without_touching_metadata(tmp_path):
    root = _repo(tmp_path)
    port = GitWorkspaceReality()
    (root / "src.py").write_text("changed\n", encoding="utf-8")
    before = port.capture(str(root))
    _git(root, "add", "src.py")
    delta = port.revalidate(str(root), before, ("src.py",))
    assert delta and delta["requires_replan"]
