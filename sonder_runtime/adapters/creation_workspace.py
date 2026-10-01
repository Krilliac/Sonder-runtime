"""Run-scoped workspaces for writing agents.

Writing agents with no explicitly selected project must never inherit the
runtime server's current directory.  Their artifacts live below the state
home in a directory dedicated to the run.  This adapter owns the small amount
of path validation needed at that boundary and has no server-module import.
<<<<<<< HEAD

The console's default session folder uses the same rules under a configured
workspace root instead (see :func:`plan_session_workspace` for why).
"""
from __future__ import annotations

import datetime
import os
import secrets
import unicodedata
=======
"""
from __future__ import annotations

import os
>>>>>>> origin/main
import uuid
import re
from pathlib import Path

from sonder_runtime.platform import paths


_DEFAULT_PROJECTS = frozenset({"", "default"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
<<<<<<< HEAD
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
=======
>>>>>>> origin/main


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


<<<<<<< HEAD
def _creation_target(
    run_id: object,
    *,
    state_home: str | Path | None,
    source_root: str | Path | None,
    home_label: str = "state home",
) -> tuple[Path, Path, Path]:
    """Check ``<home>/creations/<run-id>`` and return its parts, creating nothing.

    Returns ``(resolved home, creations, target)``.  Callers that must ask
    permission first (the console's default folder) run this before the gate,
    and :func:`_establish` runs it again before it creates anything.
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

    creations = home_resolved / "creations"
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
) -> Path:
    """Create ``<home>/creations/<run-id>`` and re-check where it landed."""
    home_resolved, creations, target = _creation_target(
        run_id, state_home=state_home, source_root=source_root, home_label=home_label,
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


=======
>>>>>>> origin/main
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
<<<<<<< HEAD
    return _establish(run_id, state_home=state_home, source_root=source_root)


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
    roots: tuple[str, ...] | list[str],
    *,
    today: datetime.date | None = None,
    source_root: str | Path | None = None,
) -> Path:
    """Choose ``<root>/creations/<name>`` for a console session, creating nothing.

    The console runs work through managed REPL work, which grants only
    ``[state].workspace_roots`` and refuses any root that overlaps private
    control state.  The state home holds that state, so a folder under
    ``<state-home>/creations`` is refused there whatever the configuration.
    The first configured root that exists and passes the creation checks
    (no link, not inside a Sonder source checkout) holds the folder instead.
    A name already on disk gets a new random suffix, so a new session never
    reuses an earlier session's folder.  Raises ``CreationWorkspaceError``
    naming why no root qualified.
    """
    reasons = []
    for raw in roots:
        text = str(raw or "").strip()
        if not text:
            continue
        root = Path(text).expanduser()
        if not root.is_absolute() or not root.is_dir():
            reasons.append("%s: not an existing directory" % text)
            continue
        for _attempt in range(8):
            try:
                _home, _creations, target = _creation_target(
                    session_workspace_name(task, today=today),
                    state_home=root, source_root=source_root, home_label="workspace root",
                )
            except CreationWorkspaceError as exc:
                reasons.append("%s: %s" % (text, exc))
                break
            if not os.path.lexists(target):
                return target
        else:
            reasons.append("%s: no unused folder name" % text)
    if not reasons:
        raise CreationWorkspaceError(
            "no workspace root is configured; add one to [state].workspace_roots in sonder.toml"
        )
    raise CreationWorkspaceError("no configured workspace root can hold one (%s)" % "; ".join(reasons))


def create_session_workspace(target: str | Path, *, source_root: str | Path | None = None) -> Path:
    """Create a folder chosen by :func:`plan_session_workspace`, checking it again."""
    planned = Path(target)
    return _establish(
        planned.name, state_home=planned.parent.parent, source_root=source_root,
        home_label="workspace root",
    )
=======

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
>>>>>>> origin/main


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
<<<<<<< HEAD
    "prepare_writing_project", "prepare_loop_project", "session_workspace_name",
    "plan_session_workspace", "create_session_workspace",
=======
    "prepare_writing_project", "prepare_loop_project",
>>>>>>> origin/main
]
