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

# Literal shared resolvers; source correspondence is checked by the manifest test.
STATE_DATABASES = (
    ("approvals.db", "SONDER_APPROVALS_DB"),
    ("jobs.db", "SONDER_JOBS_DB"),
    ("execution-spill.db", "SONDER_JOBS_DB"),
    ("child-sessions.db", "SONDER_CHILD_SESSIONS_DB"),
    ("fanout.db", "SONDER_FANOUT_DB"),
    ("extensions.db", "SONDER_EXTENSIONS_DB"),
    ("queued_actions.db", "SONDER_QUEUED_ACTION_DB"),
    ("served_action_receipts.db", "SONDER_SERVED_ACTION_RECEIPTS_DB"),
    ("operations.db", "SONDER_OPERATIONS_DB"),
    ("autopilot.db", "SONDER_AUTOPILOT_DB"),
    ("composition.db", "SONDER_COMPOSITION_DB"),
    ("updates.db", "SONDER_UPDATES_DB"),
    ("embed-cache.db", "SONDER_EMBED_CACHE_DB"),
)


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
    atomic_files: tuple[Path, ...] = ()

    def __post_init__(self):
        names = (
            "databases",
            "files",
            "owned_directories",
            "owner_lock_directories",
            "audit_files",
            "atomic_files",
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
    atomic_files: tuple[Path, ...] = ()

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
            or any(
                path.parent == target.parent
                and path.name.startswith(target.name + ".tmp-")
                for target in self.atomic_files
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
        _canonical(memory_override) if memory_override else home / "memory.db",
        (
            _canonical(Path(sessions_override).absolute())
            if sessions_override
            else state("sessions.db", "SONDER_SESSIONS_DB")
        ),
        *(state(name, variable) for name, variable in STATE_DATABASES),
    ]
    # app's task repository explicitly selects STATE_HOME/memory.db before the
    # canonical memory resolver. Both are actual composed paths.
    state_home = os.environ.get("SONDER_STATE_HOME", "").strip()
    if state_home:
        databases.append(_canonical(Path(state_home).expanduser() / "memory.db"))
    files = [state("fleet-principal.json", "SONDER_FLEET_PRINCIPAL_FILE")]
    workspace = Path(__file__).resolve().parents[3]
    roots_override = os.environ.get("SONDER_FILE_ROOTS_FILE", "").strip()
    files.extend(
        (
            _canonical(roots_override) if roots_override else home / "file_roots.local",
            workspace / "file_roots.local",
            workspace / "permissions.json",
            home / "permissions.json",
        )
    )
    # Same workspace-relative override semantics as file_ops._workspace_config_path.
    for variable, name in (
        ("SONDER_WORKFLOWS", "workflows.json"),
        ("SONDER_EMOTION_VECTORS", "emotion_vectors.json"),
        ("SONDER_SYSTEM_PROFILE", "system_profile.md"),
    ):
        raw = os.environ.get(variable, "").strip()
        candidate = Path(raw).expanduser() if raw else workspace / name
        files.append(
            _canonical(candidate if candidate.is_absolute() else workspace / candidate)
        )
    policy_override = os.environ.get("SONDER_RUNTIME_POLICY", "").strip()
    policy = (
        _canonical(policy_override) if policy_override else state("runtime_policy.json")
    )
    rotation_override = os.environ.get("SONDER_ROTATION_STATE", "").strip()
    rotation = (
        _canonical(rotation_override)
        if rotation_override
        else home / "secrets" / "rotation.json"
    )
    atomic = [
        policy,
        Path(str(policy) + ".transition.json"),
        rotation,
        state("branch_predictor.json"),
        state("npu-shadow-ledger.json", "SONDER_NPU_SHADOW_LEDGER"),
    ]
    # CLI defaults and environment paths; explicit --config/--secrets constructor
    # inputs must additionally be supplied by trusted composition.
    for variable, name in (
        ("SONDER_CONFIG", "sonder.toml"),
        ("SONDER_SECRETS", "sonder.env"),
    ):
        override = os.environ.get(variable, "").strip()
        atomic.append(
            _canonical(Path(override).absolute()) if override else home / name
        )
    atomic.append(home / "workflows.json")
    from .unsafe_lab import _audit_path

    catalog = os.environ.get("SONDER_LANE_TEST_TARGETS_FILE", "").strip()
    if catalog:
        files.append(_canonical(catalog))
    audits = [
        state(os.path.join("audit", "tool-receipts.jsonl"), "SONDER_TOOL_AUDIT"),
        _canonical(_audit_path(os.environ)),
    ]
    owned, lock_dirs = [
        home / "secrets",
        home / "terminal-output",
        home / "locks",
        state("npu-manifests", "SONDER_NPU_MANIFEST_DIR"),
    ], [fleet.parent]
    snapshots = list(_ADDITIONAL.get())
    if additional is not None:
        if not callable(additional):
            raise TypeError("trusted live inventory callback required")
        snapshots.append(additional())
    count = (
        len(databases)
        + len(files)
        + len(audits)
        + len(lock_dirs)
        + len(owned)
        + len(atomic)
    )
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
                "atomic_files",
            )
        )
        if count > 256:
            raise ValueError("combined private path inventory exceeds bound")
        databases.extend(snapshot.databases)
        files.extend(snapshot.files)
        audits.extend(snapshot.audit_files)
        owned.extend(snapshot.owned_directories)
        lock_dirs.extend(snapshot.owner_lock_directories)
        atomic.extend(snapshot.atomic_files)
    exact = {_canonical(path) for path in (*files, *audits, *atomic)}
    exact.update(_canonical(str(path) + ".lock") for path in atomic)
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
    return ControlPlaneInventory(
        frozenset(exact),
        owned,
        lock_dirs,
        audits,
        admission,
        tuple(sorted({_canonical(path) for path in atomic})),
    )
