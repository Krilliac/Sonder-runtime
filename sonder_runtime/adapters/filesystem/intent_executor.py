"""POSIX executor for race-resistant filesystem intents.

``sonder_runtime.application.security.race_resistant_paths`` decides *whether*
a destructive operation may cross the authorized-root boundary and describes
it as an :class:`OpenIntent` (or a :class:`DestructiveTarget` batch member).
It never touches the filesystem.  This adapter is the native half: it performs
the operation without trusting any pathname below the authorized root.

The walk opens the authorized root, then every intermediate component with
``O_DIRECTORY | O_NOFOLLOW`` relative to the previous directory descriptor,
so a symlink swapped into a parent component after the caller's preflight is
refused by the kernel (``ELOOP``/``ENOTDIR``) instead of being followed.  The
final component is inspected with ``fstatat(..., AT_SYMLINK_NOFOLLOW)`` and
removed with ``unlinkat``; a directory is emptied through descriptors opened
the same way, so no name inside the tree is ever re-resolved from a path.

Windows is not supported: the stdlib exposes no ``dir_fd`` operations there,
and a pathname fallback would reintroduce exactly the check/use race this
module exists to close.  Unsupported hosts raise
:class:`PlatformCapabilityError`; callers decide whether to fail closed or keep
a separately documented platform path.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from sonder_runtime.application.security.race_resistant_paths import (
    DestructiveTarget,
    OpenIntent,
    PlatformCapabilityError,
    RaceResistanceError,
)

# Each level of a recursive delete holds one directory descriptor open, so the
# depth bound is also a descriptor bound.
MAX_TREE_DEPTH = 256

EntryGuard = Callable[[Path], None]


def stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return the (device, inode, file type) triple that names one object."""

    return (int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode))


def same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return stat_identity(left) == stat_identity(right)


def require_delete_capability() -> None:
    """Raise unless every primitive the delete walk needs is available."""

    if os.name == "nt":
        raise PlatformCapabilityError(
            "descriptor-relative delete is unavailable on Windows; "
            "a native reparse-safe handle adapter is required"
        )
    missing = [
        name
        for name, function in (
            ("open", getattr(os, "open", None)),
            ("stat", getattr(os, "stat", None)),
            ("unlink", getattr(os, "unlink", None)),
            ("rmdir", getattr(os, "rmdir", None)),
        )
        if function is None or function not in os.supports_dir_fd
    ]
    if getattr(os, "scandir", None) not in os.supports_fd:
        missing.append("scandir(fd)")
    for flag in ("O_NOFOLLOW", "O_DIRECTORY"):
        if not hasattr(os, flag):
            missing.append(flag)
    if missing:
        raise PlatformCapabilityError(
            "descriptor-relative delete requires unavailable primitives: "
            + ", ".join(missing)
        )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _require_component(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise RaceResistanceError("unsafe path component in destructive intent")


def _target_anchor(target: OpenIntent | DestructiveTarget) -> tuple[Path, tuple[str, ...]]:
    if isinstance(target, OpenIntent):
        if target.operation != "delete" or not target.directory_handle_required:
            raise RaceResistanceError("intent is not a directory-handle delete")
        return target.resolution.root, target.resolution.relative_parts
    if isinstance(target, DestructiveTarget):
        return target.root, target.relative_parts
    raise TypeError("expected an OpenIntent or DestructiveTarget")


def _open_parent(root: Path, parents: tuple[str, ...]) -> int:
    """Open ``root/parents...`` one no-follow component at a time."""

    flags = _directory_flags()
    fd = os.open(str(root), flags)
    try:
        for part in parents:
            _require_component(part)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove_directory_contents(
    directory_fd: int,
    lexical: Path,
    guard: EntryGuard | None,
    depth: int,
) -> None:
    if depth > MAX_TREE_DEPTH:
        raise RaceResistanceError("recursive delete exceeds the depth bound")
    with os.scandir(directory_fd) as iterator:
        names = [entry.name for entry in iterator]
    for name in names:
        _require_component(name)
        child_path = lexical / name
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise RaceResistanceError(
                "refusing to traverse symlink during delete: %s" % child_path
            )
        if guard is not None:
            guard(child_path)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                if not same_identity(os.fstat(child_fd), info):
                    raise RaceResistanceError(
                        "directory changed during delete: %s" % child_path
                    )
                _remove_directory_contents(child_fd, child_path, guard, depth + 1)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def execute_delete(
    target: OpenIntent | DestructiveTarget,
    *,
    recursive: bool = False,
    expected: os.stat_result | None = None,
    entry_guard: EntryGuard | None = None,
) -> None:
    """Delete one authorized target through a no-follow descriptor walk.

    ``expected`` is the caller's ``lstat`` of the checked target; when given,
    the object found at execution time must be that same object.  A symlink
    at the final component is refused rather than unlinked, because a caller
    that preflighted a regular file or directory never approved removing a
    link that appeared later.  ``entry_guard`` is called with the lexical path
    of every descendant before it is removed and may raise to veto the delete.
    """

    require_delete_capability()
    root, parts = _target_anchor(target)
    if not parts:
        raise RaceResistanceError("destructive root deletion is not permitted")
    name = parts[-1]
    _require_component(name)
    parent_fd = _open_parent(root, parts[:-1])
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise RaceResistanceError("delete target became a symlink")
        if expected is not None and not same_identity(info, expected):
            raise RaceResistanceError("delete target changed after it was checked")
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return
        if not recursive:
            raise RaceResistanceError("directory delete requires recursive=True")
        directory_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        try:
            if not same_identity(os.fstat(directory_fd), info):
                raise RaceResistanceError("delete target changed after it was opened")
            _remove_directory_contents(
                directory_fd, root.joinpath(*parts), entry_guard, 1
            )
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
