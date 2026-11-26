"""Handle-validated reads of one file below a root, for the developer-workflow tools.

``diff_files``, ``rename_symbol`` and ``extract_references`` used to check a path
(reparse points, ``resolve()``, secret names) and then reopen the same spelling,
so a Windows 8.3 short name (``CREDEN~1.JSO`` for ``.credentials.json``) or a
parent directory swapped for a junction between the check and the open slipped
through. This module reuses the no-follow open and the opened-handle path
lookup of ``security_eval.repo_reader`` and applies the caller's protection
rules to the path of the handle that was actually opened:

- a component shaped like a short name (``NAME~N.EXT``) is refused outright;
- every component below the root is lstat-ed and a symlink or junction is refused;
- the file is opened without following a link in its last component;
- the handle's final path must lie inside the root (fail closed on Windows when it
  cannot be obtained) and must pass the caller's ``is_protected`` predicate;
- the bytes are read from that same descriptor, and callers build previews and
  diffs from those bytes, never from a second open of the name.

Thread affinity: stateless; safe from any thread. The descriptor is closed before
return on every path.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from ..domain.security_eval.errors import PathEscapeRefused, SourceRefused
from .security_eval import repo_reader as _reader


class ConfinedReadRefused(PermissionError):
    """The file may not be read: a short name, a link, an escape or a protected path."""


class ConfinedReadTooLarge(OSError):
    """The file is larger than the caller's byte cap."""


_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def short_name_component(root: Path | str, path: Path | str) -> str | None:
    """The first component of ``path`` below ``root`` shaped like an 8.3 short name, else None."""
    try:
        relative = os.path.relpath(str(path), str(root))
    except ValueError:
        return None
    for part in relative.split(os.sep):
        if part not in ("", ".", "..") and _reader._looks_like_short_name(part):
            return part
    return None


def read_confined_bytes(
    root: Path | str,
    path: Path | str,
    *,
    max_bytes: int,
    is_protected: Callable[[Path], bool],
) -> bytes:
    """Read the regular file ``path`` (below ``root``) through a validated, already-open handle.

    Raises ``FileNotFoundError`` when it is missing or not a regular file,
    ``ConfinedReadTooLarge`` over ``max_bytes`` and ``ConfinedReadRefused`` for
    every confinement failure.
    """
    root_text = os.path.abspath(str(root))
    current = os.path.abspath(str(path))
    if not _reader._inside(current, root_text) or _reader._norm(current) == _reader._norm(root_text):
        raise ConfinedReadRefused("the path resolves outside the root")
    short = short_name_component(root_text, current)
    if short is not None:
        raise ConfinedReadRefused(
            "component %r is shaped like a Windows 8.3 short name (NAME~N.EXT) and is never "
            "read; give the long name" % short
        )
    relative_parts = os.path.relpath(current, root_text).split(os.sep)
    walk = root_text
    for position, part in enumerate(relative_parts):
        walk = os.path.join(walk, part)
        try:
            info = os.lstat(walk)
        except OSError:
            raise FileNotFoundError("file does not exist") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
            raise ConfinedReadRefused("the path passes through a symlink or junction")
        if position < len(relative_parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise FileNotFoundError("file does not exist")
    try:
        descriptor = _reader._open_no_follow(current)
    except (PathEscapeRefused, SourceRefused) as exc:
        raise ConfinedReadRefused(str(exc)) from None
    except OSError:
        raise FileNotFoundError("file could not be opened") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileNotFoundError("not a regular file")
        _validate_handle(descriptor, opened, root_text, current, is_protected)
        if opened.st_size > max_bytes:
            raise ConfinedReadTooLarge("file is over the %d byte cap" % max_bytes)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ConfinedReadTooLarge("file is over the %d byte cap" % max_bytes)
        return data
    finally:
        os.close(descriptor)


def _validate_handle(
    descriptor: int,
    opened: os.stat_result,
    root: str,
    current: str,
    is_protected: Callable[[Path], bool],
) -> None:
    try:
        actual = _reader._opened_handle_path(descriptor)
    except PathEscapeRefused as exc:
        raise ConfinedReadRefused(str(exc)) from None
    except OSError:
        raise ConfinedReadRefused("the opened path could not be confirmed, so it is not read") from None
    if actual is None:
        if os.name == "nt":
            raise ConfinedReadRefused("the opened path could not be confirmed, so it is not read")
        if not os.path.samestat(opened, os.stat(current, follow_symlinks=False)):
            raise ConfinedReadRefused("the file changed while it was validated")
        actual = current
    if not _reader._inside(actual, root):
        raise ConfinedReadRefused("the opened file resolves outside the root")
    if _reader._handle_relative(actual, root) is None:
        raise ConfinedReadRefused("the opened path could not be placed below the root, so it is not read")
    if is_protected(Path(actual)):
        raise ConfinedReadRefused("the opened file is a secret or control-state path and is never read")
    if os.name != "nt" and not os.path.samestat(opened, os.stat(current, follow_symlinks=False)):
        raise ConfinedReadRefused("the file changed while it was validated")
