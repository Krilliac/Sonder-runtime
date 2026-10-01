"""Run-scoped workspaces for writing agents.

Writing agents with no explicitly selected project must never inherit the
runtime server's current directory.  Their artifacts live below the state
home in a directory dedicated to the run.  This adapter owns the small amount
of path validation needed at that boundary and has no server-module import.
"""
from __future__ import annotations

import os
import uuid
import re
from pathlib import Path

from sonder_runtime.platform import paths


_DEFAULT_PROJECTS = frozenset({"", "default"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CreationWorkspaceError(ValueError):
    """The requested run workspace cannot be safely established."""


def _is_default_project(project: object) -> bool:
    if project is None:
        return True
    value = str(project).strip()
    if value.casefold() in _DEFAULT_PROJECTS:
        return True
    # A bare project namespace that has not resolved to a directory is not a
    # safe filesystem root.  Path-like selectors are left to the existing
    # explicit-project flow so its historical error remains visible.
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        return False
    return not any(separator in value for separator in ("/", "\\")) and not Path(value).drive


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _under_source_checkout(path: Path, source_root: Path | None) -> bool:
    """Return whether *path* is within a known Sonder source checkout."""
    candidates = []
    if source_root is not None:
        candidates.append(Path(source_root).expanduser())
    # This module is packaged inside the checkout during development.  Walk
    # its ancestors only; never scan arbitrary filesystem roots.
    candidates.extend(Path(__file__).resolve().parents)
    candidates.append(path)
    candidates.extend(path.parents)
    for candidate in candidates:
        try:
            root = candidate.resolve(strict=False)
        except OSError:
            continue
        if not (root / ".git").exists():
            continue
        if source_root is None and not (
            (root / "sonder_runtime").is_dir() or (root / "server.py").is_file()
        ):
            continue
        if _is_within(path, root):
            return True
    return False


def _validate_run_id(run_id: object) -> str:
    value = str(run_id or "").strip()
    if not _SAFE_RUN_ID.fullmatch(value):
        raise CreationWorkspaceError("run id must be one safe path component")
    return value


def resolve_writing_workspace(
    project: object,
    run_id: object,
    *,
    state_home: str | Path | None = None,
    source_root: str | Path | None = None,
) -> Path:
    """Resolve a writing agent's project without inheriting the server cwd.

    An explicit project selector is returned unchanged as a ``Path``.  A
    default, empty, or unresolved selector gets a new
    ``<state-home>/creations/<run-id>`` directory.  The generated path is
    resolved and checked against the state home after creation so existing
    symlinks cannot redirect artifacts outside the state home.
    """
    if not _is_default_project(project):
        return Path(str(project)).expanduser()

    identifier = _validate_run_id(run_id)
    home = Path(state_home).expanduser() if state_home is not None else paths.default_home()
    try:
        home_resolved = home.resolve(strict=False)
    except OSError as exc:
        raise CreationWorkspaceError("state home cannot be resolved") from exc
    if _under_source_checkout(home_resolved, Path(source_root) if source_root else None):
        raise CreationWorkspaceError("state home cannot be inside a Sonder source checkout")

    creations = home_resolved / "creations"
    target = creations / identifier
    try:
        # Check before creating anything: ``mkdir(exist_ok=True)`` would
        # otherwise follow an attacker-planted symlink and briefly write into
        # an arbitrary directory before the postcondition check below.
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        if creations.is_symlink() or target.is_symlink() or is_junction(str(creations)) or is_junction(str(target)):
            raise CreationWorkspaceError("creation workspace uses a symlink")
        resolved_creations = creations.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        if _under_source_checkout(resolved_target, Path(source_root) if source_root else None):
            raise CreationWorkspaceError("creation workspace cannot be inside a Sonder source checkout")
        if not _is_within(resolved_creations, home_resolved) or not _is_within(
            resolved_target, resolved_creations
        ):
            raise CreationWorkspaceError("creation workspace escaped its state home")
        home_resolved.mkdir(parents=True, exist_ok=True)
        creations.mkdir(parents=True, exist_ok=True)
        target.mkdir(exist_ok=True)
        resolved_creations = creations.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
    except CreationWorkspaceError:
        raise
    except OSError as exc:
        raise CreationWorkspaceError("creation workspace cannot be established") from exc
    if not _is_within(resolved_creations, home_resolved) or not _is_within(
        resolved_target, resolved_creations
    ):
        raise CreationWorkspaceError("creation workspace escaped its state home")
    if _under_source_checkout(resolved_target, Path(source_root) if source_root else None):
        raise CreationWorkspaceError("creation workspace cannot be inside a Sonder source checkout")
    return resolved_target


def writing_project(
    project: object,
    run_id: object,
    *,
    state_home: str | Path | None = None,
    source_root: str | Path | None = None,
) -> str:
    """Return the project value to persist for a writing run.

    Explicit selectors remain byte-for-byte string selectors (including
    relative spelling and trailing separators).  Default-like selectors are
    replaced by the newly created run-scoped workspace.
    """
    if not _is_default_project(project):
        return str(project)
    return str(
        resolve_writing_workspace(
            project,
            run_id,
            state_home=state_home,
            source_root=source_root,
        )
    )


def prepare_writing_project(project: object) -> tuple[str, str]:
    """Bind one default workspace before a standalone agent opens its lanes."""
    try:
        return writing_project(project, "agent-" + uuid.uuid4().hex[:12]), ""
    except (OSError, ValueError) as exc:
        return "", "workspace request failed: %s" % exc


def prepare_loop_project(project: object, *, writing: bool) -> tuple[object, str]:
    """Inside the agent loop, upgrade only a *named* project that names no directory.

    The entrypoints (``agent``, the workbench, autopilot start) already map an
    omitted, default or unresolved project before the loop runs.  An omitted
    project reaching the loop is the unbound contract of host-owned callers
    that keep their own roots: the selfmod editor works in its candidate
    workspace under a policy that refuses host-injected ``extra_roots``, the
    web research agent writes nothing, and unsafe lab clears the project on
    purpose.  Binding those to a fresh creations folder refused every selfmod
    file call.  A named label (``default``, an unknown project name) is still
    upgraded so it can never fall back to the server's working directory.
    """
    if writing and str(project or "").strip():
        return prepare_writing_project(project)
    return project, ""


__all__ = [
    "CreationWorkspaceError", "resolve_writing_workspace", "writing_project",
    "prepare_writing_project", "prepare_loop_project",
]
