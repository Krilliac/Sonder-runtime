"""Refuse a developer tool whose writes would reach a protected control-plane path.

The general file tools refuse protected paths one name at a time
(``file_ops._require_mutation_access``).  Three developer tools write files
they choose themselves, and none of them takes a developer token:

* ``rename_symbol`` rewrites every file its glob matched that holds the
  symbol.  :func:`files_refusal` gives each of those files the file tools'
  guard (which also refuses git metadata, a hard-linked file and every other
  name of a protected file), and one refused file refuses the whole rename.
* ``format_code`` and ``lint_run(fix=True)`` hand a path (or the root) to a
  formatter that picks the files under it.  :func:`scope_refusal` refuses
  when that scope

  - is itself refused by the file tools' guard;
  - holds, or lies inside, a protected place: an exact control-plane file
    (``file_ops._control_plane_paths``, the live inventory and the model tool
    policy files), a directory whose whole content is protected (the package
    data directory, the inventory's owned directories, Sonder's first-party
    code directories under the workspace root), or the workspace root itself
    (its root-level modules and launchers); or
  - holds a symbolic link or junction that leads to such a place or to a
    protected file (a formatter may follow it).  The walk follows no link,
    skips git metadata directories (no formatter writes there) and refuses
    once it has seen ``MAX_SCOPE_ENTRIES`` entries.

  - holds a hard link to a protected file (a formatter writes through it).
    A file with one name has no other name, so when no protected file has
    more than one link (the usual case, one ``stat`` each) nothing under the
    scope can be one, and the scope is not scanned.  Only when some protected
    file does have another name is the scope walked, each regular file with
    more than one link compared by identity (``os.path.samestat``) with the
    protected files.  Unrelated hard links (a pnpm store, a uv environment)
    never refuse.

  A user's project outside those places keeps its formatter.  The scope check
  does not apply the file tools' name rules to every file under the scope (a
  ``.env`` or a ``*.pem`` anywhere): formatters do not rewrite those, and a
  per-file control-plane check over a whole tree costs a full inventory read
  per file.

A refusal names the path relative to the tool's root, never the host path.
Nothing is cached; the functions hold no state and may be called from any
thread.  Like every pathname check, the check and the child's writes are two
steps.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.operator_only_paths as _operator_only
from sonder_runtime.adapters.security.control_plane_paths import live_control_plane_inventory

MAX_SCOPE_ENTRIES = 200_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _ScopeUnchecked(Exception):
    """The scope could not be walked completely."""


def _relative(root: Path, path: Path) -> str:
    try:
        text = Path(path).relative_to(root).as_posix()
    except ValueError:
        text = Path(path).name
    return text or "."


def _message(operation: str, relative: str) -> str:
    return (
        "refusing %s: it would change the protected Sonder control-plane path %s "
        "(refused without an authenticated developer token)" % (operation, relative)
    )


def files_refusal(operation: str, root: Path, paths: Iterable[Path]) -> str:
    """Why ``operation`` may not write ``paths``, or ``""`` when none is protected."""
    for path in paths:
        try:
            file_ops._require_mutation_access(Path(path), False)
        except PermissionError:
            return _message(operation, _relative(Path(root), Path(path)))
    return ""


class _Places:
    """The protected places a formatter scope is compared with, read once per check."""

    def __init__(self) -> None:
        inventory = live_control_plane_inventory()
        root = file_ops._resolve_best_effort(file_ops.workspace_root())
        policy = _operator_only.locate(file_ops.RUNTIME_PACKAGE_DATA_DIR, file_ops._resolve_best_effort)
        self.root = root
        self.exact = set(file_ops._control_plane_paths()) | set(inventory.exact_files) | set(policy.exact)
        self.exact.update(root / parent / name for parent, name in file_ops.FIRST_PARTY_CODE_FILES)
        self.whole = {file_ops._resolve_best_effort(file_ops.RUNTIME_PACKAGE_DATA_DIR)}
        self.whole.update(Path(directory) for directory in inventory.owned_directories)
        self.whole.update(
            root / name for name in (*file_ops.FIRST_PARTY_CODE_DIRS, *file_ops.RUNTIME_PACKAGE_DIRS)
        )

    def reached_from(self, place: Path) -> bool:
        """Whether a formatter given ``place`` (resolved, ordinary spelling) may rewrite a protected file."""
        if place == self.root or place in self.root.parents:
            return True
        if any(item == place or place in item.parents for item in self.exact):
            return True
        return any(item == place or place in item.parents or item in place.parents for item in self.whole)


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _links_under(scope: Path) -> Iterator[Path]:
    """Every symbolic link or junction under ``scope`` (not followed); raises :class:`_ScopeUnchecked`."""
    pending = [scope]
    seen = 0
    git_metadata = {name.casefold() for name in file_ops.GIT_METADATA_DIRECTORIES}
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                listed = list(entries)
        except OSError as exc:
            raise _ScopeUnchecked("could not list %s (%s)" % (current.name, type(exc).__name__)) from exc
        for entry in listed:
            seen += 1
            if seen > MAX_SCOPE_ENTRIES:
                raise _ScopeUnchecked("more than %d entries" % MAX_SCOPE_ENTRIES)
            try:
                if _is_link(entry):
                    yield Path(entry.path)
                elif entry.is_dir(follow_symlinks=False) and entry.name.casefold() not in git_metadata:
                    pending.append(Path(entry.path))
            except OSError as exc:
                raise _ScopeUnchecked("could not inspect %s (%s)" % (entry.name, type(exc).__name__)) from exc


def _linked_protected_stats(places: _Places) -> list[os.stat_result]:
    """``os.stat`` of each protected file that has more than one name (usually none)."""
    protected = set(file_ops._identity_protected_files()) | set(places.exact)
    found: list[os.stat_result] = []
    for path in protected:
        try:
            status = os.stat(path)
        except (OSError, ValueError):
            continue  # a missing file has no other name; the editor creates it by atomic replace
        if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
            found.append(status)
    return found


def _hard_link_under(scope: Path, protected: list[os.stat_result]) -> Path | None:
    """A regular file under ``scope`` that is another name for one of ``protected``; raises :class:`_ScopeUnchecked`."""
    pending = [scope]
    seen = 0
    git_metadata = {name.casefold() for name in file_ops.GIT_METADATA_DIRECTORIES}
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                listed = list(entries)
        except OSError as exc:
            raise _ScopeUnchecked("could not list %s (%s)" % (current.name, type(exc).__name__)) from exc
        for entry in listed:
            seen += 1
            if seen > MAX_SCOPE_ENTRIES:
                raise _ScopeUnchecked("more than %d entries" % MAX_SCOPE_ENTRIES)
            try:
                if _is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in git_metadata:
                        pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    status = os.lstat(entry.path)  # DirEntry.stat reports no link count on Windows
                    if status.st_nlink > 1 and any(os.path.samestat(status, item) for item in protected):
                        return Path(entry.path)
            except OSError as exc:
                raise _ScopeUnchecked("could not inspect %s (%s)" % (entry.name, type(exc).__name__)) from exc
    return None


def _spelled(path: Path) -> Path | None:
    try:
        return file_ops._protection_spelling(path)
    except (OSError, ValueError):
        return None


def scope_refusal(operation: str, root: Path, scope: Path) -> str:
    """Why a formatter run over ``scope`` (a file or directory under ``root``) may not run, or ``""``."""
    root = Path(root)
    scope = Path(scope)
    relative = _relative(root, scope)
    try:
        file_ops._require_mutation_access(scope, False)
    except PermissionError:
        return _message(operation, relative)
    spelled = _spelled(scope)
    places = _Places()
    if spelled is None or places.reached_from(spelled):
        return _message(operation, relative)
    if not spelled.is_dir():
        return ""
    try:
        for link in _links_under(spelled):
            target = _spelled(link)
            if target is None or file_ops._is_protected_mutation_path(target) or places.reached_from(target):
                return _message(operation, _relative(root, link))
        linked = _linked_protected_stats(places)
        if linked:
            alias = _hard_link_under(spelled, linked)
            if alias is not None:
                return _message(operation, _relative(root, alias))
    except _ScopeUnchecked as exc:
        return (
            "refusing %s: the files under %s could not all be checked against the protected Sonder "
            "control-plane paths (%s); pass a narrower path" % (operation, relative, exc)
        )
    return ""


__all__ = ["MAX_SCOPE_ENTRIES", "files_refusal", "scope_refusal"]
