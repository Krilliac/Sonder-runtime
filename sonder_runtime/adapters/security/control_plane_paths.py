"""Live bounded host control-state paths, without opening or creating stores."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fnmatch
from itertools import islice
import os
from pathlib import Path
import re

from ...platform import paths as runtime_paths


def _canonical(value):
    path = Path(value).expanduser()
    if len(str(path).encode()) > 4096:
        raise ValueError("private path exceeds bound")
    return path.resolve()


@dataclass(frozen=True)
class ControlPlanePaths:
    """Explicit trusted constructor paths; no guessed store defaults."""

    databases: tuple[Path, ...] = ()
    files: tuple[Path, ...] = ()
    owned_directories: tuple[Path, ...] = ()
    owner_lock_directories: tuple[Path, ...] = ()
    audit_files: tuple[Path, ...] = ()

    def __post_init__(self):
        names = (
            "databases",
            "files",
            "owned_directories",
            "owner_lock_directories",
            "audit_files",
        )
        if any(not isinstance(getattr(self, name), tuple) for name in names):
            raise ValueError("immutable private path tuples required")
        if sum(len(getattr(self, name)) for name in names) > 256:
            raise ValueError("private path inventory exceeds bound")
        for name in names:
            values = getattr(self, name)
            if any(not Path(value).is_absolute() for value in values):
                raise ValueError("absolute private paths required")
            object.__setattr__(
                self, name, tuple(sorted({_canonical(value) for value in values}))
            )


@dataclass(frozen=True)
class ControlPlaneInventory:
    exact_files: frozenset[Path]
    owned_directories: tuple[Path, ...]
    owner_lock_directories: tuple[Path, ...]
    audit_files: tuple[Path, ...]
    admission_directories: tuple[Path, ...]

    def protects(self, path):
        path = _canonical(path)
        return (
            path in self.exact_files
            or any(
                path == root or root in path.parents for root in self.owned_directories
            )
            or (
                path.parent in self.owner_lock_directories
                and re.fullmatch(r"lane-owner-[0-9a-f]{32}\.lock", path.name)
                is not None
            )
            or any(
                path.parent == audit.parent
                and fnmatch.fnmatchcase(path.name, audit.stem + ".*" + audit.suffix)
                for audit in self.audit_files
            )
        )

    def require_disjoint(self, model_roots):
        roots = tuple(islice(model_roots, 257))
        if len(roots) > 256:
            raise ValueError("workspace inventory exceeds bound")
        for value in roots:
            root = _canonical(value)
            if any(
                root == private or root in private.parents or private in root.parents
                for private in self.admission_directories
            ):
                raise PermissionError("private control state overlaps model workspace")


_ADDITIONAL = ContextVar("host_control_plane_paths", default=())


@contextmanager
def control_plane_scope(paths):
    """Host-only immutable supplemental paths; default protection always remains."""
    if not isinstance(paths, ControlPlanePaths):
        raise TypeError("typed private path snapshot required")
    prior = _ADDITIONAL.get()
    if len(prior) >= 16:
        raise ValueError("private path scope nesting exceeds bound")
    token = _ADDITIONAL.set((*prior, paths))
    try:
        yield paths
    finally:
        _ADDITIONAL.reset(token)


def live_control_plane_inventory(*, additional=None):
    """Read actual state-path precedence without state_path's mkdir/migration IO.

    additional is a trusted live callback returning ControlPlanePaths for stores
    constructed with explicit paths (e.g. terminal output), not request fields.
    """
    configured = runtime_paths._configured_home()
    home = _canonical(runtime_paths.default_home())

    def state(name, variable=""):
        if configured is not None:
            return _canonical(configured / name)
        override = os.environ.get(variable, "").strip() if variable else ""
        return _canonical(override if override else home / name)

    memory_override = os.environ.get("SONDER_DB", "").strip()
    sessions_override = os.environ.get("SONDER_SESSIONS_DB", "").strip()
    fleet = state("fleet.db", "SONDER_FLEET_DB")
    databases = [
        fleet,
        state("approvals.db", "SONDER_APPROVALS_DB"),
        _canonical(memory_override) if memory_override else home / "memory.db",
        (
            _canonical(sessions_override)
            if sessions_override
            else state("sessions.db", "SONDER_SESSIONS_DB")
        ),
        state("jobs.db", "SONDER_JOBS_DB"),
        state("execution-spill.db", "SONDER_JOBS_DB"),
        state("child-sessions.db", "SONDER_CHILD_SESSIONS_DB"),
        state("fanout.db", "SONDER_FANOUT_DB"),
        state("extensions.db", "SONDER_EXTENSIONS_DB"),
    ]
    # app's task repository explicitly selects STATE_HOME/memory.db before the
    # canonical memory resolver. Both are actual composed paths.
    state_home = os.environ.get("SONDER_STATE_HOME", "").strip()
    if state_home:
        databases.append(_canonical(Path(state_home).expanduser() / "memory.db"))
    files = [state("fleet-principal.json", "SONDER_FLEET_PRINCIPAL_FILE")]
    catalog = os.environ.get("SONDER_LANE_TEST_TARGETS_FILE", "").strip()
    if catalog:
        files.append(_canonical(catalog))
    audits = [state(os.path.join("audit", "tool-receipts.jsonl"), "SONDER_TOOL_AUDIT")]
    owned, lock_dirs = [], [fleet.parent]
    snapshots = list(_ADDITIONAL.get())
    if additional is not None:
        if not callable(additional):
            raise TypeError("trusted live inventory callback required")
        snapshots.append(additional())
    count = len(databases) + len(files) + len(audits) + len(lock_dirs)
    for snapshot in snapshots:
        if not isinstance(snapshot, ControlPlanePaths):
            raise TypeError("typed private path snapshot required")
        count += sum(
            len(getattr(snapshot, name))
            for name in (
                "databases",
                "files",
                "owned_directories",
                "owner_lock_directories",
                "audit_files",
            )
        )
        if count > 256:
            raise ValueError("combined private path inventory exceeds bound")
        databases.extend(snapshot.databases)
        files.extend(snapshot.files)
        audits.extend(snapshot.audit_files)
        owned.extend(snapshot.owned_directories)
        lock_dirs.extend(snapshot.owner_lock_directories)
    exact = {_canonical(path) for path in (*files, *audits)}
    for database in databases:
        exact.update(
            _canonical(str(database) + suffix)
            for suffix in ("", "-wal", "-shm", "-journal")
        )
    owned = tuple(sorted({_canonical(path) for path in owned}))
    lock_dirs = tuple(sorted({_canonical(path) for path in lock_dirs}))
    audits = tuple(sorted({_canonical(path) for path in audits}))
    admission = tuple(
        sorted({path.parent for path in exact} | set(owned) | set(lock_dirs))
    )
    return ControlPlaneInventory(frozenset(exact), owned, lock_dirs, audits, admission)
