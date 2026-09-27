"""Host executable guards and model-facing path redaction.

The inventory must never launch a binary that project code could have
planted.  A path inside a configured file root (the workspace a model can
write) is "project-local" and is recorded but never executed.  Roots that are
the filesystem root, the user's home, or an ancestor of home are ignored for
this purpose: treating them as project roots would classify every installed
tool as project-local.  That is also this guard's known limitation -- a
home-wide file root is not defended (see docs/host-tool-inventory.md).

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


def _project_roots() -> list[str]:
    home = _home()
    roots: list[str] = []
    for root in file_ops.allowed_roots():
        resolved = _resolve(str(root))
        if Path(resolved).anchor == resolved or Path(resolved).parent == Path(resolved):
            continue  # filesystem root
        if os.path.normcase(resolved) == os.path.normcase(home) or _is_inside(home, resolved):
            continue  # home or an ancestor of home
        roots.append(resolved)
    return roots


def project_local(path: str) -> bool:
    """Whether *path* (lexically or resolved) lies inside a project file root."""
    if not isinstance(path, str) or not path:
        return False
    lexical = os.path.normpath(os.path.abspath(path))
    resolved = _resolve(path)
    for root in _project_roots():
        if _is_inside(lexical, root) or _is_inside(resolved, root):
            return True
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
