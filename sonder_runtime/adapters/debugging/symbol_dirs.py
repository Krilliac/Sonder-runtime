"""Containment for operator symbol directories.

The lexical refusals (``*``, ``;``, quotes, control characters, ``srv*`` /
``symsrv*`` / ``cache*`` syntax, URL schemes, UNC and device paths, ``..``,
relative paths) are pure and live in ``domain.debugging.symbol_path``. What
needs the filesystem is here: the directory must lie inside the allowed file
roots, be a real directory with no symlink or junction on its path (its
realpath re-resolves to the same place), and -- on Windows -- sit on a drive
that is not a mapped network share (``GetDriveTypeW == DRIVE_REMOTE``): a
debugger reading symbols from a network drive is egress.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Callable

from ...application.debugging.ports import SYMBOL_PATH_REJECTED, debug_error
from ..filesystem import file_ops

DRIVE_REMOTE = 4
MAX_SYMBOL_DIRS = 8


def _drive_type_windows(root: str) -> int:  # pragma: no cover - Windows only
    import ctypes

    function = ctypes.windll.kernel32.GetDriveTypeW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint
    return int(function(root))


def _reject(message: str) -> Exception:
    return debug_error(SYMBOL_PATH_REJECTED, message)


def contain_symbol_dir(path: str, extra_roots: str = "", *, system: str | None = None,
                       drive_type: Callable[[str], int] | None = None,
                       exists: bool = True) -> str:
    """The lexically valid, contained, non-remote directory, or ``SYMBOL_PATH_REJECTED``.

    ``system`` defaults to this host (``"Windows"`` enables the drive check);
    ``drive_type`` is injectable so the Windows rules are testable anywhere.
    ``exists=False`` stops after the lexical and drive checks (planning on a
    host that does not have the directory, e.g. tests of Windows plans).
    """
    from ...domain.debugging.symbol_path import SymbolPathRejected, lexical_symbol_dir

    host = system or ("Windows" if os.name == "nt" else "Linux")
    try:
        lexical = lexical_symbol_dir(path, system=host)
    except SymbolPathRejected as exc:
        raise _reject(str(exc)) from None
    windows = host.lower().startswith("win")
    if windows:
        drive = PureWindowsPath(lexical).drive
        checker = drive_type or (_drive_type_windows if os.name == "nt" else None)
        if checker is None:
            raise _reject("cannot verify the drive type of %s" % drive)
        try:
            kind = int(checker(drive + "\\"))
        except Exception:
            raise _reject("cannot verify the drive type of %s" % drive) from None
        if kind == DRIVE_REMOTE:
            raise _reject("symbol dirs on mapped network drives are refused (egress)")
    if not exists:
        return lexical
    candidate = Path(lexical)
    try:
        file_ops._require_no_reparse_components(candidate)
        resolved = candidate.resolve(strict=True)
    except (OSError, PermissionError, ValueError) as exc:
        raise _reject("symbol dir is missing or crosses a link: %s" % type(exc).__name__) from None
    if os.path.normcase(os.path.normpath(str(resolved))) != os.path.normcase(os.path.normpath(lexical)):
        raise _reject("symbol dir resolves somewhere else (link or junction)")
    if not resolved.is_dir():
        raise _reject("symbol dir is not a directory")
    if not file_ops.inside_allowed_roots(resolved, extra_roots):
        raise _reject("symbol dir is outside the allowed file roots")
    if file_ops.credential_read_component(resolved):
        raise _reject("symbol dir is a credential store")
    return str(resolved)


def contain_symbol_dirs(paths, extra_roots: str = "", **kwargs) -> tuple[str, ...]:
    values = [str(item) for item in (paths or ())]
    if len(values) > MAX_SYMBOL_DIRS:
        raise _reject("at most %d symbol dirs" % MAX_SYMBOL_DIRS)
    out: list[str] = []
    for value in values:
        contained = contain_symbol_dir(value, extra_roots, **kwargs)
        if contained not in out:
            out.append(contained)
    return tuple(out)


__all__ = ["DRIVE_REMOTE", "MAX_SYMBOL_DIRS", "contain_symbol_dir", "contain_symbol_dirs"]
