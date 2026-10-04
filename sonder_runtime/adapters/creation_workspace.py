"""Run-scoped workspaces for work started without a project.

Work with no explicitly selected project must never inherit the runtime
server's current directory.  It gets a folder of its own under the app-owned
default workspace root: ``%USERPROFILE%\\Sonder\\workspaces`` on Windows,
``~/Sonder/workspaces`` elsewhere, or ``[state].default_workspace_root``.  Like
Codex's and Claude's managed workspaces the root is per user, visible, and
outside Sonder's state home, so the console and the app put creations in one
place.  Managed console work grants that root by design
(``bootstrap/repl_managed.py``), so it must pass every check below.  When it
cannot be used (no user home, or a service account whose home is the state
home), project-less writing runs fall back to ``<state-home>/creations``.
This adapter owns the path validation at that boundary and has no
server-module import.
"""
from __future__ import annotations

import datetime
import logging
import os
import secrets
import unicodedata
import uuid
import re
from pathlib import Path

from sonder_runtime.platform import paths


_LOG = logging.getLogger(__name__)
_ROOT_LABEL = "default workspace root"
_DEFAULT_PROJECTS = frozenset({"", "default"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A session folder name keeps a few words of the request that opened it.
_SLUG_WORDS = 4
_SLUG_CHARS = 32
# Words that say how something was asked for rather than what it is, so
# "can you make a cool webpage" names its folder "cool-webpage".
_SLUG_FILLER = frozenset((
    "a an the this that these those some any it its "
    "i me my we us our you your "
    "can could would will should shall may might must please pls kindly "
    "let lets just also now then hey hi hello "
    "make create build write generate code develop implement produce "
    "do give get set put start up help need want like new "
    "something thing stuff "
    "for to of in on at by with and or from into as"
).split())


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


def _creation_target(
    run_id: object,
    *,
    state_home: str | Path | None,
    source_root: str | Path | None,
    home_label: str = "state home",
    folder: str = "creations",
) -> tuple[Path, Path, Path]:
    """Check ``<home>/<folder>/<run-id>`` and return its parts, creating nothing.

    Returns ``(resolved home, folder, target)``.  The default workspace root
    uses its parent as *home* and its own name as *folder*, so the root gets
    the link check a creations folder gets.  Callers that must ask permission
    first (the console's default folder) run this before the gate, and
    :func:`_establish` runs it again before it creates anything.
    """
    identifier = _validate_run_id(run_id)
    home = Path(state_home).expanduser() if state_home is not None else paths.default_home()
    source = Path(source_root) if source_root else None
    try:
        home_resolved = home.resolve(strict=False)
    except OSError as exc:
        raise CreationWorkspaceError("%s cannot be resolved" % home_label) from exc
    if _under_source_checkout(home_resolved, source):
        raise CreationWorkspaceError("%s cannot be inside a Sonder source checkout" % home_label)

    creations = home_resolved / folder
    target = creations / identifier
    try:
        # Check before creating anything: ``mkdir(exist_ok=True)`` would
        # otherwise follow an attacker-planted symlink and briefly write into
        # an arbitrary directory before the postcondition check in _establish.
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        if creations.is_symlink() or target.is_symlink() or is_junction(str(creations)) or is_junction(str(target)):
            raise CreationWorkspaceError("creation workspace uses a symlink")
        resolved_creations = creations.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        if _under_source_checkout(resolved_target, source):
            raise CreationWorkspaceError("creation workspace cannot be inside a Sonder source checkout")
        if not _is_within(resolved_creations, home_resolved) or not _is_within(
            resolved_target, resolved_creations
        ):
            raise CreationWorkspaceError("creation workspace escaped its %s" % home_label)
    except CreationWorkspaceError:
        raise
    except OSError as exc:
        raise CreationWorkspaceError("creation workspace cannot be established") from exc
    return home_resolved, creations, target


def _establish(
    run_id: object,
    *,
    state_home: str | Path | None,
    source_root: str | Path | None,
    home_label: str = "state home",
    folder: str = "creations",
) -> Path:
    """Create ``<home>/<folder>/<run-id>`` and re-check where it landed."""
    home_resolved, creations, target = _creation_target(
        run_id, state_home=state_home, source_root=source_root, home_label=home_label,
        folder=folder,
    )
    try:
        home_resolved.mkdir(parents=True, exist_ok=True)
        creations.mkdir(parents=True, exist_ok=True)
        target.mkdir(exist_ok=True)
        resolved_creations = creations.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        raise CreationWorkspaceError("creation workspace cannot be established") from exc
    if not _is_within(resolved_creations, home_resolved) or not _is_within(
        resolved_target, resolved_creations
    ):
        raise CreationWorkspaceError("creation workspace escaped its %s" % home_label)
    if _under_source_checkout(resolved_target, Path(source_root) if source_root else None):
        raise CreationWorkspaceError("creation workspace cannot be inside a Sonder source checkout")
    return resolved_target


def default_workspace_root(
    configured: str | Path | None = "",
    *,
    source_root: str | Path | None = None,
    inventory=None,
) -> Path:
    """Resolve and check the app-owned default workspace root, creating nothing.

    *configured* is ``[state].default_workspace_root``; empty means
    :func:`paths.default_workspace_root` (``SONDER_DEFAULT_WORKSPACE_ROOT``,
    else ``%USERPROFILE%\\Sonder\\workspaces`` or ``~/Sonder/workspaces``).
    Managed console work grants this root without it being listed in
    ``[state].workspace_roots``, so it must be an absolute folder that is not a
    link, not inside a Sonder source checkout, and clear of all private
    control state: the state home, its stores, and the running checkout.
    *inventory* is the caller's control-plane snapshot (the admission passes
    its own, which includes constructor-owned paths).  Raises
    ``CreationWorkspaceError`` naming the check that failed.
    """
    text = str(configured or "").strip()
    try:
        raw = Path(text).expanduser() if text else paths.default_workspace_root()
    except RuntimeError:
        raw = None
    if raw is None:
        raise CreationWorkspaceError(
            "no user home is known to hold it; set [state].default_workspace_root"
        )
    if not raw.is_absolute() or not raw.name:
        raise CreationWorkspaceError("%s must be an absolute folder path: %s" % (_ROOT_LABEL, raw))
    is_junction = getattr(os.path, "isjunction", lambda value: False)
    try:
        if raw.is_symlink() or is_junction(str(raw)):
            raise CreationWorkspaceError("%s is a link: %s" % (_ROOT_LABEL, raw))
        resolved = raw.resolve(strict=False)
    except CreationWorkspaceError:
        raise
    except OSError as exc:
        raise CreationWorkspaceError("%s cannot be resolved: %s" % (_ROOT_LABEL, raw)) from exc
    if _under_source_checkout(resolved, Path(source_root) if source_root else None):
        raise CreationWorkspaceError(
            "%s cannot be inside a Sonder source checkout: %s" % (_ROOT_LABEL, resolved)
        )
    try:
        if inventory is None:
            from sonder_runtime.adapters.security.control_plane_paths import (
                live_control_plane_inventory,
            )

            inventory = live_control_plane_inventory()
        inventory.require_disjoint((resolved,))
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise CreationWorkspaceError(
            "%s overlaps Sonder's private control state (the state home, its stores, "
            "or the running source checkout): %s" % (_ROOT_LABEL, resolved)
        ) from exc
    return resolved


def default_workspace_grant(configured: object, inventory, *, source_root=None) -> Path | None:
    """The default workspace root managed work may grant, or None.

    None when the root does not exist yet or fails any check in
    :func:`default_workspace_root`; it is then simply not granted, so a bad
    default root never refuses work in a configured root.
    """
    try:
        root = default_workspace_root(configured, source_root=source_root, inventory=inventory)
    except CreationWorkspaceError:
        return None
    return root if root.is_dir() else None


def _plan_default_folder(name_for, *, configured, source_root) -> Path:
    root = default_workspace_root(configured, source_root=source_root)
    for _attempt in range(8):
        _home, _root, target = _creation_target(
            name_for(), state_home=root.parent, source_root=source_root,
            home_label=_ROOT_LABEL, folder=root.name,
        )
        if not os.path.lexists(target):
            return target
    raise CreationWorkspaceError("no unused folder name in %s" % root)


def _create_default_folder(target: Path, *, configured, source_root) -> Path:
    root = default_workspace_root(configured, source_root=source_root)
    if Path(target).parent != root:
        raise CreationWorkspaceError("planned folder is not in the %s" % _ROOT_LABEL)
    created = _establish(
        Path(target).name, state_home=root.parent, source_root=source_root,
        home_label=_ROOT_LABEL, folder=root.name,
    )
    # The root may not have existed when it was checked; check what is there.
    default_workspace_root(configured, source_root=source_root)
    return created


def resolve_writing_workspace(
    project: object,
    run_id: object,
    *,
    state_home: str | Path | None = None,
    source_root: str | Path | None = None,
    task: object = "",
) -> Path:
    """Resolve a writing agent's project without inheriting the server cwd.

    An explicit project selector is returned unchanged as a ``Path``.  A
    default, empty, or unresolved selector gets a new folder in the default
    workspace root, named from *task* like a console session's folder (or
    after *run_id* when there is no task).  When that root is unusable, or
    *state_home* is given, it gets ``<state-home>/creations/<run-id>`` as
    before.  Generated paths are resolved and re-checked after creation so
    existing links cannot redirect artifacts elsewhere.
    """
    if not _is_default_project(project):
        return Path(str(project)).expanduser()
    identifier = _validate_run_id(run_id)
    if state_home is None:
        def name_for():
            return session_workspace_name(task) if str(task or "").strip() else identifier

        try:
            target = _plan_default_folder(name_for, configured="", source_root=source_root)
            return _create_default_folder(target, configured="", source_root=source_root)
        except CreationWorkspaceError as exc:
            # A service account whose home is the state home (the packaged
            # Linux unit) has no usable default root; keep working there.
            _LOG.warning("default workspace root unusable, using the state home: %s", exc)
    return _establish(identifier, state_home=state_home, source_root=source_root)


def session_workspace_name(
    task: object,
    *,
    today: datetime.date | None = None,
    token: str | None = None,
) -> str:
    """``<YYYY-MM-DD>-<a few words of task>-<4 hex>``: one safe path component.

    The words are folded to ASCII, request filler is dropped, and an empty
    result (a request in a script with no ASCII letters) reads ``work``.
    """
    folded = unicodedata.normalize("NFKD", str(task or "")).encode("ascii", "ignore").decode("ascii")
    words = [
        word for word in re.findall(r"[a-z0-9]+", folded.lower())
        if word not in _SLUG_FILLER and not (len(word) == 1 and word.isalpha())
    ]
    slug = ""
    for word in words[:_SLUG_WORDS]:
        joined = "%s-%s" % (slug, word) if slug else word
        if len(joined) > _SLUG_CHARS:
            slug = slug or word[:_SLUG_CHARS]
            break
        slug = joined
    day = (today or datetime.date.today()).isoformat()
    return _validate_run_id("%s-%s-%s" % (day, slug or "work", token or secrets.token_hex(2)))


def plan_session_workspace(
    task: object,
    *,
    configured: str | Path | None = "",
    today: datetime.date | None = None,
    source_root: str | Path | None = None,
) -> Path:
    """Choose ``<default root>/<YYYY-MM-DD>-<slug>-<4 hex>``, creating nothing.

    The console plans the folder first so the ``/workspace-create`` gate is
    asked about the exact path before anything exists.  A name already on
    disk gets a new suffix, so a new session never reuses an earlier
    session's folder.  Raises ``CreationWorkspaceError`` naming why the
    default root cannot hold it.
    """
    return _plan_default_folder(
        lambda: session_workspace_name(task, today=today),
        configured=configured, source_root=source_root,
    )


def create_session_workspace(
    target: str | Path,
    *,
    configured: str | Path | None = "",
    source_root: str | Path | None = None,
) -> Path:
    """Create a folder chosen by :func:`plan_session_workspace`, checking it again."""
    return _create_default_folder(Path(target), configured=configured, source_root=source_root)


def writing_project(
    project: object,
    run_id: object,
    *,
    state_home: str | Path | None = None,
    source_root: str | Path | None = None,
    task: object = "",
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
            task=task,
        )
    )


def prepare_writing_project(project: object, task: object = "") -> tuple[str, str]:
    """Bind one default workspace before a standalone agent opens its lanes."""
    try:
        return writing_project(project, "agent-" + uuid.uuid4().hex[:12], task=task), ""
    except (OSError, ValueError) as exc:
        return "", "workspace request failed: %s" % exc


def prepare_loop_project(project: object, *, writing: bool, task: object = "") -> tuple[object, str]:
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
        return prepare_writing_project(project, task)
    return project, ""


__all__ = [
    "CreationWorkspaceError", "resolve_writing_workspace", "writing_project",
    "prepare_writing_project", "prepare_loop_project", "session_workspace_name",
    "default_workspace_root", "default_workspace_grant",
    "plan_session_workspace", "create_session_workspace",
]
