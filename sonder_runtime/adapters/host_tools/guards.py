"""Host executable guards and model-facing path redaction.

The inventory must never launch a binary that project code could have
planted.  A path inside a configured file root (the workspace a model can
write) is "project-local" and is recorded but never executed.  Roots that are
the filesystem root, the user's home, or an ancestor of home are "broad":
treating everything under them as project-local would classify every
installed tool as planted.  Under a broad root a path is instead
project-local when this user could modify it (write, delete, or re-ACL the
file, or replace it in its directory) -- a binary the runtime cannot modify
cannot have been planted through a file tool.  Probe failures count as
modifiable, so the check fails closed.

These checks are re-run at launch time (``require_host_executable``) because
the persisted snapshot lives in a state home a model may be able to write.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import stat
from typing import Callable

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.host_tools.model import is_absolute_host_path, redact_path

_WINDOWS_APPS_MARKER = "\\microsoft\\windowsapps\\"


def is_windows_apps_alias(path: str) -> bool:
    """True for a Store execution alias (``...\\Microsoft\\WindowsApps\\x.exe``).

    Aliases are zero-byte reparse points that may open the Store or a stub
    instead of the named tool, so they are recorded but never executed.
    """
    if not isinstance(path, str):
        return False
    folded = path.replace("/", "\\").casefold()
    return _WINDOWS_APPS_MARKER in folded


def _resolve(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(os.path.abspath(path))


def _is_inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(child), os.path.normcase(parent)]) == os.path.normcase(parent)
    except ValueError:
        return False


def _home() -> str:
    return _resolve(os.path.expanduser("~"))


def _classified_roots() -> tuple[list[str], list[str]]:
    """(project roots, broad roots) of the configured writable file roots."""
    home = _home()
    roots: list[str] = []
    broad: list[str] = []
    for root in file_ops.allowed_roots():
        resolved = _resolve(str(root))
        if (
            Path(resolved).anchor == resolved
            or Path(resolved).parent == Path(resolved)  # filesystem root
            or os.path.normcase(resolved) == os.path.normcase(home)
            or _is_inside(home, resolved)  # home or an ancestor of home
        ):
            broad.append(resolved)
        else:
            roots.append(resolved)
    return roots, broad


def _project_roots() -> list[str]:
    return _classified_roots()[0]


# Windows access rights probed by ``_windows_modifiable``.
_FILE_WRITE_DATA = 0x0002  # FILE_ADD_FILE on a directory
_FILE_DELETE_CHILD = 0x0040
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_ERROR_ACCESS_DENIED = 5
_SHARE_ALL = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | WRITE | DELETE
_OPEN_EXISTING = 3
# FILE_FLAG_BACKUP_SEMANTICS (open directories) | FILE_FLAG_OPEN_REPARSE_POINT
_OPEN_FLAGS = 0x02000000 | 0x00200000


def _windows_can_open(path: str, access: int) -> bool:
    """Whether this token is granted *access* on *path*; errors count as yes."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, OSError, AttributeError):
        return True
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        path, access, _SHARE_ALL, None, _OPEN_EXISTING, _OPEN_FLAGS, None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        # Access is checked before sharing, so any other refusal (a sharing
        # violation on a running image included) does not show the right is
        # missing -- fail closed.
        return ctypes.get_last_error() != _ERROR_ACCESS_DENIED
    kernel32.CloseHandle(handle)
    return True


def _windows_modifiable(path: str) -> bool:
    for access in (_FILE_WRITE_DATA, _DELETE, _WRITE_DAC, _WRITE_OWNER):
        if _windows_can_open(path, access):
            return True
    parent = os.path.dirname(path)
    if parent and parent != path:
        for access in (_FILE_DELETE_CHILD, _WRITE_DAC, _WRITE_OWNER):
            if _windows_can_open(parent, access):
                return True
    return False


def _posix_modifiable(path: str) -> bool:
    geteuid = getattr(os, "geteuid", None)
    candidates = [path]
    parent = os.path.dirname(path)
    if parent and parent != path:
        candidates.append(parent)
    for candidate in candidates:
        try:
            info = os.stat(candidate)
        except (OSError, ValueError):
            return True
        if geteuid is not None and info.st_uid == geteuid():
            return True  # the owner can chmod it writable
        if os.access(candidate, os.W_OK):
            return True
    return False


def _modifiable_by_this_user(path: str) -> bool:
    """Whether this runtime user could have planted or can replace *path*."""
    if os.name == "nt":
        return _windows_modifiable(path)
    return _posix_modifiable(path)


def project_local(path: str) -> bool:
    """Whether *path* (lexically or resolved) lies inside a writable file root.

    Inside a project root that is always the case. Inside a broad root (the
    filesystem root, home, or an ancestor of home) it is the case when this
    user could modify *path* -- see the module docstring.
    """
    if not isinstance(path, str) or not path:
        return False
    lexical = os.path.normpath(os.path.abspath(path))
    resolved = _resolve(path)
    roots, broad = _classified_roots()
    for root in roots:
        if _is_inside(lexical, root) or _is_inside(resolved, root):
            return True
    if any(_is_inside(lexical, root) or _is_inside(resolved, root) for root in broad):
        return _modifiable_by_this_user(resolved) or (
            lexical != resolved and _modifiable_by_this_user(lexical)
        )
    return False


def executable_allowed(path: str) -> bool:
    """Absolute, existing regular executable that is neither planted nor an alias."""
    if not is_absolute_host_path(path) or "\x00" in path:
        return False
    if is_windows_apps_alias(path):
        return False
    resolved = _resolve(path)
    try:
        info = os.stat(resolved)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        return False
    return not project_local(path)


def require_host_executable(path: str) -> str:
    """Return *path* if it may be launched as a host tool, else raise."""
    if not executable_allowed(path):
        raise PermissionError("host executable rejected")
    return path


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def display_redactor() -> Callable[[str], str]:
    """Build ``redact_path`` bound to this host's home, user and file roots."""
    home = os.path.expanduser("~")
    user = _user()
    roots: list[str] = []
    try:
        for root in file_ops.allowed_roots():
            text = str(root)
            if Path(text).anchor != text:
                roots.append(text)
    except Exception:
        roots = []
    workspace_roots = tuple(roots)

    def redact(path: str) -> str:
        return redact_path(path, home=home, user=user, workspace_roots=workspace_roots)

    return redact


__all__ = [
    "display_redactor",
    "executable_allowed",
    "is_windows_apps_alias",
    "project_local",
    "require_host_executable",
]
