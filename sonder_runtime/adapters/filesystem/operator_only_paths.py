"""The model tool policy files, which only the attended operator console changes.

The shipped policy (``<package data>/model_tool_policy.json``, the tool-lane
floor) and the operator overlay (``model_tool_policy.overlay_path``, with the
``.lock`` and ``.tmp-`` siblings its editor uses) decide which tools a model
may call.  The design rule is that only the operator, at the console, changes
them: no MCP tool parameter, HTTP route or agent dispatch.  A developer token
is such a parameter (``server._file_developer_allowed``), so unlike the rest
of the control-plane state it does not open these files:
``file_ops._require_mutation_access`` asks :func:`is_policy_file` before it
honours the token, and a recursive delete asks :func:`holds_policy_file`.

Paths are compared in their resolved, ordinary spelling (the caller resolves
and normalizes first).  A name the comparison can miss -- a UNC spelling of a
local folder, or another hard link to the same file (``path_aliases``) -- is
mapped back through a known directory and compared by file identity.

A name next to the overlay that starts like it is refused too, with or
without a developer token: ``model_tool_policy*`` (the default name) or the
overlay's own stem when ``SONDER_MODEL_TOOL_POLICY`` renames it, in the
overlay's folder and in the state home.  The overlay does not exist until an
operator creates it, so a path that does not exist cannot be compared by
identity; a backup, a variant spelling or a lock file written there would be
read by nothing today and by something tomorrow.

Nothing is cached: :func:`locate` reads the overlay location (which follows
``SONDER_MODEL_TOOL_POLICY``) on every call.  The functions hold no state and
are safe from any thread; like every pathname check, a check and the write
after it are two steps.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import sonder_runtime.adapters.filesystem.path_aliases as _path_aliases
import sonder_runtime.adapters.model_tool_policy as _model_tool_policy
from sonder_runtime.platform import paths as _runtime_paths

LOCK_SUFFIX = ".lock"
TEMP_MARKER = ".tmp-"


def _folded(name: str) -> str:
    # Case-insensitive on every platform: ``os.path.normcase`` folds nothing on
    # POSIX, yet a case-insensitive volume (macOS APFS by default, a mounted
    # NTFS/FAT/SMB share) makes ``MODEL_TOOL_POLICY.json`` the overlay itself,
    # and a case variant is a policy-file spelling everywhere else.
    return os.path.normcase(name).casefold()


@dataclass(frozen=True)
class PolicyFiles:
    """Where the two policy files are, resolved; ``overlay`` is None when its location cannot be read."""

    shipped: Path
    overlay: Path | None
    state_dir: Path | None = None

    @property
    def exact(self) -> tuple[Path, ...]:
        """The shipped file, the overlay and the overlay's lock file."""
        if self.overlay is None:
            return (self.shipped,)
        return (self.shipped, self.overlay, self.overlay.with_name(self.overlay.name + LOCK_SUFFIX))

    def names(self, path: Path) -> bool:
        """Whether ``path`` (ordinary spelling) is one of the files or an overlay ``.tmp-`` sibling."""
        if path in self.exact:
            return True
        return self._starts_like_the_overlay(path)

    def _starts_like_the_overlay(self, path: Path) -> bool:
        """Whether ``path`` is in the overlay's folder or the state home with a name that starts like the overlay's."""
        overlay = self.overlay
        folders = [folder for folder in (overlay.parent if overlay is not None else None, self.state_dir) if folder]
        prefixes = {_folded(Path(_model_tool_policy.OVERLAY_FILE_NAME).stem)}
        if overlay is not None:
            prefixes.add(_folded(overlay.stem))
        folded_parent = _folded(str(path.parent))
        name = _folded(path.name)
        return any(_folded(str(folder)) == folded_parent for folder in folders) and any(
            name.startswith(prefix) for prefix in prefixes if prefix
        )

    def under(self, path: Path) -> bool:
        """Whether ``path`` (ordinary spelling) is a directory above one of the files."""
        return any(path in candidate.parents for candidate in self.exact)


def locate(package_data_dir: Path, resolve: Callable[[Path], Path]) -> PolicyFiles:
    """The policy files: the shipped one inside ``package_data_dir``, the overlay where the runtime reads it."""
    shipped = resolve(Path(package_data_dir) / _model_tool_policy.SHIPPED_PATH.name)
    try:
        overlay: Path | None = resolve(_model_tool_policy.overlay_path())
    except (OSError, ValueError, TypeError):
        # The runtime cannot read an overlay it cannot locate either, and then
        # refuses every tool lane (OVERLAY_UNUSABLE); there is no file to guard.
        overlay = None
    try:
        state_dir: Path | None = resolve(Path(_runtime_paths.state_path(_model_tool_policy.OVERLAY_FILE_NAME)).parent)
    except (OSError, ValueError, TypeError):
        state_dir = None
    return PolicyFiles(shipped=shipped, overlay=overlay, state_dir=state_dir)


def _ancestors(paths: Iterable[Path]) -> set[Path]:
    found: set[Path] = set()
    for path in paths:
        found.update(path.parents)
    return found


def is_policy_file(path: Path, files: PolicyFiles) -> bool:
    """Whether ``path`` (resolved, ordinary spelling) is a policy file under any of its names."""
    if files.names(path):
        return True
    if not _path_aliases.may_alias(path):
        return False
    local = _path_aliases.local_equivalent(path, {candidate.parent for candidate in files.exact})
    if local is not None and local != path and files.names(local):
        return True
    return _path_aliases.same_file_as_any(path, files.exact)


def holds_policy_file(path: Path, files: PolicyFiles) -> bool:
    """Whether the directory ``path`` (resolved, ordinary spelling) holds a policy file, under any of its names."""
    if files.under(path):
        return True
    if not _path_aliases.may_alias(path):
        return False
    local = _path_aliases.local_equivalent(path, _ancestors(files.exact))
    return local is not None and local != path and files.under(local)


__all__ = ["LOCK_SUFFIX", "PolicyFiles", "TEMP_MARKER", "holds_policy_file", "is_policy_file", "locate"]
