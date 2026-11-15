"""Other names for one file: Windows namespace spellings, UNC shares and hard links.

The protected-path checks in ``file_ops`` decide by *name*: the resolved path
is compared with the control-plane inventory, the package data directory and
a few name rules.  A file has other names those comparisons do not see:

* on Windows, ``\\\\?\\D:\\x`` (the extended-length namespace) and
  ``\\\\?\\UNC\\host\\share\\x`` name the same files as ``D:\\x`` and
  ``\\\\host\\share\\x``; ``Path.resolve`` keeps the prefix, so the name
  comparison missed them.  :func:`normal_spelling` maps them to the ordinary
  spelling with the routine the control-plane inventory itself uses
  (``control_plane_paths.normalized_final_path``) and raises ``ValueError``
  for a namespace that has no ordinary spelling (``\\\\.\\``, ``GLOBALROOT``),
  which callers treat as protected;
* a UNC spelling of a local folder (``\\\\localhost\\D$\\x``) and a hard link
  inside an allowed project are different names for the same file.  For a
  path that may be such an alias (:func:`may_alias`), :func:`local_equivalent`
  finds the ordinary name through the identity of a known directory above it,
  and :func:`same_file_as_any` compares the file's identity (device and file
  index, ``os.path.samestat``) with the protected files themselves.

Everything here reads file metadata only (``os.stat``); nothing is opened for
writing and nothing is cached, so every call sees the disk as it is now.  The
functions hold no state and are safe from any thread.  A check and the write
that follows it are still two steps (the same window ``file_ops`` documents
for its other pathname checks).
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from sonder_runtime.adapters.security.control_plane_paths import normalized_final_path

# Ancestors inspected when mapping an alias to a known directory.
_MAX_ANCESTORS = 64


def normal_spelling(path: Path) -> Path:
    """``path`` with a Windows ``\\\\?\\`` or ``\\\\.\\`` prefix mapped to its ordinary spelling.

    Raises ``ValueError`` when the namespace has no ordinary spelling.  Any
    other path, and every path off Windows, is returned unchanged.
    """
    if os.name != "nt":
        return path
    text = os.fspath(path)
    if not text.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    return normalized_final_path(Path(text))


def _stat(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except (OSError, ValueError):
        return None


def hard_link_count(path: Path) -> int:
    """How many names the regular file at ``path`` has; 0 when it is missing or not a regular file."""
    status = _stat(path)
    if status is None or not stat.S_ISREG(status.st_mode):
        return 0
    return int(status.st_nlink)


def _is_unc(path: Path) -> bool:
    return os.name == "nt" and Path(path).drive.startswith("\\\\")


def may_alias(path: Path) -> bool:
    """Whether a name check alone can miss that ``path`` is another name for a protected file.

    True for a UNC spelling on Windows and for a regular file with more than
    one name (a hard link).
    """
    return _is_unc(path) or hard_link_count(path) > 1


def local_equivalent(path: Path, anchors: Iterable[Path]) -> Path | None:
    """The ordinary name of ``path`` under one of ``anchors``, found by directory identity.

    The nearest existing directory above ``path`` (or ``path`` itself) that is
    the same directory as an anchor gives ``anchor / <the rest of path>``.
    None when no ancestor is one of the anchors.
    """
    known = [(anchor, status) for anchor in anchors if (status := _stat(anchor)) is not None]
    if not known:
        return None
    current = Path(path)
    for _ in range(_MAX_ANCESTORS):
        status = _stat(current)
        if status is not None:
            for anchor, anchor_status in known:
                if os.path.samestat(status, anchor_status):
                    return anchor.joinpath(*Path(path).parts[len(current.parts):])
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def same_file_as_any(path: Path, candidates: Iterable[Path]) -> bool:
    """Whether the existing file at ``path`` is the same file as one of ``candidates``."""
    status = _stat(path)
    if status is None:
        return False
    for candidate in candidates:
        other = _stat(candidate)
        if other is not None and os.path.samestat(status, other):
            return True
    return False


__all__ = ["hard_link_count", "local_equivalent", "may_alias", "normal_spelling", "same_file_as_any"]
