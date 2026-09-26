"""Race-resistant delete: the POSIX intent executor and its delete_path consumer.

Each race test swaps a filesystem object *between* the pathname preflight and
the descriptor-relative execution by wrapping the seam where one hands over to
the other, then proves the executor refused instead of following the swap.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.filesystem import intent_executor
from sonder_runtime.application.security.race_resistant_paths import (
    PlatformCapabilityError,
    RaceResistanceError,
    build_open_intent,
    check_destructive_targets,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="descriptor-relative delete races are characterized on Linux",
)


@pytest.fixture()
def workspace(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: state)
    return root


def _confirmed_delete(path: Path, **kwargs):
    preview = file_ops.delete_path(str(path), **kwargs)
    return file_ops.delete_path(
        str(path), dry_run=False, confirm=preview["required_confirm"], **kwargs
    )


def _swap_after_intent(monkeypatch, swap):
    """Run ``swap`` after the delete intent is built, before it executes."""

    def build_then_swap(*args, **kwargs):
        intent = build_open_intent(*args, **kwargs)
        swap()
        return intent

    monkeypatch.setattr(file_ops, "build_open_intent", build_then_swap)


def test_intermediate_symlink_swap_after_preflight_is_refused(
    monkeypatch, workspace, tmp_path
):
    real_parent = workspace / "a" / "b"
    real_parent.mkdir(parents=True)
    target = real_parent / "target.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    victim = outside / "b" / "target.txt"
    victim.write_text("outside", encoding="utf-8")
    preview = file_ops.delete_path(str(target))

    def swap():
        (workspace / "a").rename(workspace / "a-moved")
        (workspace / "a").symlink_to(outside, target_is_directory=True)

    _swap_after_intent(monkeypatch, swap)
    with pytest.raises(PermissionError, match="race-safely") as caught:
        file_ops.delete_path(
            str(target), dry_run=False, confirm=preview["required_confirm"]
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno in {errno.ELOOP, errno.ENOTDIR}
    assert victim.read_text(encoding="utf-8") == "outside"
    assert (workspace / "a-moved" / "b" / "target.txt").exists()


def test_executor_refuses_symlinked_intermediate_component(tmp_path):
    root = tmp_path / "root"
    (root / "dir").mkdir(parents=True)
    target = root / "dir" / "target.txt"
    target.write_text("inside", encoding="utf-8")
    intent = build_open_intent(target, [root], "delete")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "target.txt"
    victim.write_text("outside", encoding="utf-8")
    (root / "dir").rename(root / "dir-moved")
    (root / "dir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError) as caught:
        intent_executor.execute_delete(intent)

    assert caught.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert victim.exists()


def test_normal_delete_removes_only_the_in_root_target(workspace, tmp_path):
    nested = workspace / "notes"
    nested.mkdir()
    target = nested / "gone.txt"
    target.write_text("delete me", encoding="utf-8")
    sibling = nested / "kept.txt"
    sibling.write_text("keep", encoding="utf-8")
    outside = tmp_path / "gone.txt"
    outside.write_text("outside", encoding="utf-8")

    result = _confirmed_delete(target)

    assert result["deleted"] is True
    assert not target.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_recursive_delete_removes_plain_tree(workspace):
    tree = workspace / "scratch"
    (tree / "one" / "two").mkdir(parents=True)
    (tree / "one" / "two" / "leaf.txt").write_text("x", encoding="utf-8")
    (tree / "top.txt").write_text("y", encoding="utf-8")
    keep = workspace / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    result = _confirmed_delete(tree, recursive=True)

    assert result["deleted"] is True
    assert not tree.exists()
    assert keep.exists()


def test_recursive_delete_does_not_follow_symlink_swapped_in_during_walk(
    monkeypatch, workspace, tmp_path
):
    tree = workspace / "project"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "inner.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "inner.txt"
    victim.write_text("outside", encoding="utf-8")
    preview = file_ops.delete_path(str(tree), recursive=True)
    original_guard = file_ops._guard_delete_entry
    swapped = []

    def guard_then_swap(child: Path) -> None:
        original_guard(child)
        if child == tree / "sub" and not swapped:
            # The executor has already lstat'ed this entry as a directory;
            # rebind the name to a symlink before it opens the entry.
            (tree / "sub").rename(workspace / "sub-moved")
            (tree / "sub").symlink_to(outside, target_is_directory=True)
            swapped.append(child)

    monkeypatch.setattr(file_ops, "_guard_delete_entry", guard_then_swap)
    with pytest.raises(PermissionError, match="race-safely") as caught:
        file_ops.delete_path(
            str(tree), recursive=True, dry_run=False,
            confirm=preview["required_confirm"],
        )

    assert swapped
    assert caught.value.__cause__.errno in {errno.ELOOP, errno.ENOTDIR}
    assert victim.read_text(encoding="utf-8") == "outside"
    assert (tree / "sub").is_symlink()


def test_recursive_delete_refuses_directory_rebound_during_walk(
    monkeypatch, workspace
):
    tree = workspace / "project"
    (tree / "sub").mkdir(parents=True)
    replacement = workspace / "replacement"
    replacement.mkdir()
    (replacement / "precious.txt").write_text("keep", encoding="utf-8")
    preview = file_ops.delete_path(str(tree), recursive=True)
    original_guard = file_ops._guard_delete_entry

    def guard_then_rebind(child: Path) -> None:
        original_guard(child)
        if child == tree / "sub" and replacement.exists():
            (tree / "sub").rmdir()
            replacement.rename(tree / "sub")

    monkeypatch.setattr(file_ops, "_guard_delete_entry", guard_then_rebind)
    with pytest.raises(PermissionError, match="directory changed during delete"):
        file_ops.delete_path(
            str(tree), recursive=True, dry_run=False,
            confirm=preview["required_confirm"],
        )

    assert (tree / "sub" / "precious.txt").read_text(encoding="utf-8") == "keep"


def test_target_replaced_after_check_is_refused(monkeypatch, workspace):
    target = workspace / "target.txt"
    target.write_text("checked", encoding="utf-8")
    preview = file_ops.delete_path(str(target))

    def replace_target():
        replacement = workspace / "replacement.txt"
        replacement.write_text("new object", encoding="utf-8")
        os.replace(replacement, target)

    _swap_after_intent(monkeypatch, replace_target)
    with pytest.raises(PermissionError, match="changed after it was checked"):
        file_ops.delete_path(
            str(target), dry_run=False, confirm=preview["required_confirm"]
        )

    assert target.read_text(encoding="utf-8") == "new object"


def test_final_component_swapped_to_symlink_is_not_unlinked(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("inside", encoding="utf-8")
    intent = build_open_intent(target, [root], "delete")
    target.unlink()
    target.symlink_to(tmp_path / "elsewhere.txt")

    with pytest.raises(RaceResistanceError, match="became a symlink"):
        intent_executor.execute_delete(intent)

    assert target.is_symlink()


def test_unsupported_dir_fd_platform_fails_closed(monkeypatch, workspace):
    target = workspace / "target.txt"
    target.write_text("keep", encoding="utf-8")
    preview = file_ops.delete_path(str(target))
    monkeypatch.setattr(
        os, "supports_dir_fd", frozenset(os.supports_dir_fd - {os.unlink})
    )

    with pytest.raises(PlatformCapabilityError, match="unlink"):
        file_ops.delete_path(
            str(target), dry_run=False, confirm=preview["required_confirm"]
        )

    assert target.read_text(encoding="utf-8") == "keep"


def test_missing_fd_scandir_fails_closed(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("keep", encoding="utf-8")
    intent = build_open_intent(target, [root], "delete")
    monkeypatch.setattr(os, "supports_fd", frozenset(os.supports_fd - {os.scandir}))

    with pytest.raises(PlatformCapabilityError, match="scandir"):
        intent_executor.execute_delete(intent)

    assert target.exists()


def test_destructive_target_batch_executes_and_root_is_refused(tmp_path):
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    first = root / "one.txt"
    first.write_text("1", encoding="utf-8")
    (root / "d" / "two.txt").write_text("2", encoding="utf-8")

    for target in check_destructive_targets([first, root / "d"], [root]):
        intent_executor.execute_delete(target, recursive=True)

    assert sorted(os.listdir(root)) == []
    with pytest.raises(RaceResistanceError, match="root deletion"):
        intent_executor.execute_delete(
            build_open_intent(root, [root], "delete"), recursive=True
        )
    assert root.is_dir()


def test_non_delete_intent_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("keep", encoding="utf-8")

    with pytest.raises(RaceResistanceError, match="not a directory-handle delete"):
        intent_executor.execute_delete(build_open_intent(target, [root], "read"))

    assert target.exists()


def test_over_deep_tree_is_refused_before_anything_is_removed(
    monkeypatch, workspace
):
    # A small bound keeps the per-entry protected-path guard cheap; the
    # executor reads the module constant at call time.
    monkeypatch.setattr(intent_executor, "MAX_TREE_DEPTH", 8)
    tree = workspace / "deep"
    tree.mkdir()
    first = tree / "a_first.txt"
    first.write_text("keep", encoding="utf-8")
    chain = tree
    for _ in range(intent_executor.MAX_TREE_DEPTH + 4):
        chain = chain / "d"
    chain.mkdir(parents=True)
    (chain / "leaf.txt").write_text("leaf", encoding="utf-8")

    with pytest.raises(PermissionError, match="depth bound"):
        _confirmed_delete(tree, recursive=True)

    assert first.read_text(encoding="utf-8") == "keep"
    assert (chain / "leaf.txt").read_text(encoding="utf-8") == "leaf"


def test_tree_at_the_depth_bound_is_deleted(monkeypatch, workspace):
    monkeypatch.setattr(intent_executor, "MAX_TREE_DEPTH", 8)
    tree = workspace / "bounded"
    chain = tree
    for _ in range(intent_executor.MAX_TREE_DEPTH - 1):
        chain = chain / "d"
    chain.mkdir(parents=True)
    (chain / "leaf.txt").write_text("leaf", encoding="utf-8")

    result = _confirmed_delete(tree, recursive=True)

    assert result["deleted"] is True
    assert not tree.exists()


def test_executor_refuses_a_nested_symlink_without_partial_removal(tmp_path):
    root = tmp_path / "root"
    tree = root / "tree"
    (tree / "z_later").mkdir(parents=True)
    first = tree / "a_first.txt"
    first.write_text("keep", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree / "z_later" / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RaceResistanceError, match="symlink"):
        intent_executor.execute_delete(
            build_open_intent(tree, [root], "delete"), recursive=True
        )

    assert first.read_text(encoding="utf-8") == "keep"
    assert outside.is_dir()
