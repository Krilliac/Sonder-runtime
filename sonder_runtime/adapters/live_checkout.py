"""The live runtime checkout, and the rule that models do not change it.

Production is fast-forwarded from origin/main and self-modification has its
own reviewed pipeline, so a model-driven change to the checkout the runtime is
running from (a checkout, a branch switch, a merge, a stash, a patch, a
formatter) is never wanted: it could swap the shipped model tool policy, or any
other control-plane file, for another commit's.  Guessing which paths one
operation writes (and which commit a ref names) proved bypassable, so the rule
is structural: such a tool does not run in the live checkout unless the
operator typed that tool at the console (:mod:`operator_attendance`).

* :func:`checkout_of` finds the working tree a path belongs to, the way git
  does: the nearest ancestor holding a ``.git`` directory (with ``HEAD``,
  ``objects`` and ``refs``) or a ``.git`` file (a linked worktree or a
  submodule).  No git process runs.
* :func:`is_live` is true when that working tree is the same directory (device
  and file index, so junctions, symlinks and case differences do not matter)
  as the working tree of a control-plane location: the runtime's source root,
  the package data directory, the operator overlay's folder or the Sonder home.
  A linked worktree has its own ``.git`` file, so it is not live; neither is a
  nested repository.
* :func:`refusal` is the guard the tools call.

Nothing is cached; every call reads the disk as it is.  The functions hold no
state and may be called from any thread.  The cost is a few ``stat`` calls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.model_tool_policy as _model_tool_policy
import sonder_runtime.adapters.operator_attendance as _attendance
from sonder_runtime.platform import paths as runtime_paths

# Ancestors inspected when looking for a repository marker.
MAX_ANCESTORS = 64
_GIT_ENTRY = ".git"
_GIT_DIRECTORY_MEMBERS = ("HEAD", "objects", "refs")


class LiveCheckoutGuard(str, Enum):
    """The ``guard`` field of a refused result."""

    LIVE_CHECKOUT = "live_checkout_attendance"


def _is_git_marker(directory: Path) -> bool:
    entry = directory / _GIT_ENTRY
    try:
        if entry.is_file():
            return True
        return entry.is_dir() and all((entry / member).exists() for member in _GIT_DIRECTORY_MEMBERS)
    except (OSError, ValueError):
        return False


def checkout_of(path: Path) -> Path | None:
    """The working tree ``path`` (resolved) is in, or None outside a repository."""
    current = Path(path)
    for _ in range(MAX_ANCESTORS):
        if _is_git_marker(current):
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _control_locations() -> list[Path]:
    locations = [file_ops.workspace_root(), file_ops.RUNTIME_PACKAGE_DATA_DIR]
    try:
        locations.append(_model_tool_policy.overlay_path())
    except (OSError, ValueError, TypeError):
        pass
    try:
        locations.append(Path(runtime_paths.default_home()))
    except (OSError, ValueError, TypeError):
        pass
    return [file_ops._resolve_best_effort(Path(location)) for location in locations]


def _stat(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except (OSError, ValueError):
        return None


def is_live(path: Path) -> bool:
    """Whether ``path`` lies in the working tree of one of the runtime's control-plane locations."""
    own = checkout_of(file_ops._resolve_best_effort(Path(path)))
    own_status = _stat(own) if own is not None else None
    if own_status is None:
        return False
    for location in _control_locations():
        tree = checkout_of(location)
        status = _stat(tree) if tree is not None else None
        if status is not None and os.path.samestat(own_status, status):
            return True
    return False


@dataclass(frozen=True)
class LiveCheckoutRefusal:
    """An unattended ``operation`` was refused in the live checkout."""

    operation: str

    @property
    def message(self) -> str:
        return (
            "refusing %s: the live Sonder runtime checkout is protected; it changes only when the "
            "operator types the command at the console (no developer token opens it)" % self.operation
        )

    def as_result(self) -> dict:
        """The dict shape the ``harness_tools`` functions return for a refusal."""
        return {"ok": False, "error": self.message, "guard": LiveCheckoutGuard.LIVE_CHECKOUT.value}


def refusal(operation: str, path: Path) -> LiveCheckoutRefusal | None:
    """The refusal for ``operation`` run in ``path``, or None when it may run.

    It may run when the operator typed ``operation`` at the console, or when
    ``path`` is not in the live checkout.  No subprocess runs.
    """
    if _attendance.attended_for(operation):
        return None
    return LiveCheckoutRefusal(operation) if is_live(path) else None


__all__ = ["LiveCheckoutGuard", "LiveCheckoutRefusal", "checkout_of", "is_live", "refusal"]
