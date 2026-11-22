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
    ("execution-spill.db", "SONDER_SPILL_DB"),
    ("child-sessions.db", "SONDER_CHILD_SESSIONS_DB"),
    ("fanout.db", "SONDER_FANOUT_DB"),
    ("extensions.db", "SONDER_EXTENSIONS_DB"),
    ("queued_actions.db", "SONDER_QUEUED_ACTION_DB"),
    ("served_action_receipts.db", "SONDER_SERVED_ACTION_RECEIPTS_DB"),
    ("http_work_runs.db", "SONDER_HTTP_WORK_RUNS_DB"),
    ("operations.db", "SONDER_OPERATIONS_DB"),
    ("autopilot.db", "SONDER_AUTOPILOT_DB"),
    ("composition.db", "SONDER_COMPOSITION_DB"),
    ("updates.db", "SONDER_UPDATES_DB"),
    ("embed-cache.db", "SONDER_EMBED_CACHE_DB"),
    ("agent-effects.db", "SONDER_AGENT_EFFECTS_DB"),
    ("worker-effects.db", "SONDER_WORKER_EFFECTS_DB"),
    ("model-request-admission.sqlite3", ""),
)

_WINDOWS_FORBIDDEN_COMPONENT_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    (
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{suffix}" for suffix in "0123456789¹²³"),
        *(f"LPT{suffix}" for suffix in "0123456789¹²³"),
    )
)


def _canonical(value):
    path = Path(value).expanduser()
    try:
        encoded = os.fsencode(path)
    except (UnicodeError, TypeError, ValueError):
        raise ValueError("private path encoding is unavailable") from None
    if len(encoded) > 4096:
        raise ValueError("private path exceeds bound")
    for _ in range(3):
        resolved = _normalize_windows_extended_final_path(path.resolve())
        # NTFS may move a concurrently deleted, still-open SQLite sidecar to
        # this volume-root namespace while Path.resolve holds its handle. It
        # is not the stable name of the requested private resource. Re-resolve
        # the original path; never substitute its unresolved spelling or use
        # the tombstone as authority. Persistent churn remains fail-closed.
        if (
            os.name != "nt"
            or len(resolved.drive) != 2
            or tuple(part.casefold() for part in resolved.parts[1:3])
            != ("$extend", "$deleted")
        ):
            return resolved
    raise ValueError("private path resolution is unstable")


def _normal_windows_components(value: str, *, minimum: int = 0) -> tuple[str, ...]:
    """Return components whose normal Win32 spelling preserves their identity."""
    parts = value.split("\\") if value else []
    if parts and not parts[-1]:
        parts.pop()
    if len(parts) < minimum or any(not part for part in parts):
        raise ValueError("unsupported Windows private path namespace")
    for part in parts:
        if (
            part in (".", "..")
            or part[-1] in ". "
            or any(
                ord(character) < 32
                or character in _WINDOWS_FORBIDDEN_COMPONENT_CHARS
                for character in part
            )
        ):
            raise ValueError("unsupported Windows private path namespace")
        basename = part.split(".", 1)[0].rstrip(". ").upper()
        if basename in _WINDOWS_RESERVED_DEVICE_BASENAMES:
            raise ValueError("unsupported Windows private path namespace")
    return tuple(parts)


def _normalize_windows_extended_final_path(resolved: Path) -> Path:
    """Unify only post-resolution extended paths with a safe normal spelling."""
    if os.name != "nt":
        return resolved
    value = os.fspath(resolved)
    if value.startswith("\\\\.\\"):
        raise ValueError("unsupported Windows private path namespace")
    if not value.startswith("\\\\?\\"):
        return resolved
    tail = value[4:]
    if tail[:4].casefold() == "unc\\":
        parts = _normal_windows_components(tail[4:], minimum=2)
        normal = Path("\\\\" + "\\".join(parts))
        if not normal.is_absolute():
            raise ValueError("unsupported Windows private path namespace")
        return normal
    if (
        len(tail) < 3
        or tail[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        or tail[1] != ":"
        or tail[2] != "\\"
    ):
        raise ValueError("unsupported Windows private path namespace")
    parts = _normal_windows_components(tail[3:])
    normal = Path(tail[:3] + "\\".join(parts))
    if not normal.is_absolute():
        raise ValueError("unsupported Windows private path namespace")
    return normal


def normalized_final_path(resolved: Path) -> Path:
    """The ordinary spelling of a Windows ``\\\\?\\`` path, the same way the inventory sees it.

    For callers that compare paths with this inventory by name (the file
    tools' protected-path checks).  Raises ``ValueError`` for a namespace with
    no ordinary spelling; off Windows the path is returned unchanged.
    """
    return _normalize_windows_extended_final_path(Path(resolved))


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

    def covers(self, paths):
        """Return whether this snapshot contains every explicitly supplied path.

        The check is deliberately limited to immutable, trusted composition
        paths. It lets a caller reuse one live snapshot without replacing the
        per-request canonicalization performed by :meth:`protects`.
        """
        if type(paths) is not ControlPlanePaths:
            return False
        exact = {
            _canonical(value)
            for value in (*paths.files, *paths.audit_files, *paths.atomic_files)
        }
        exact.update(
            _canonical(str(database) + suffix)
            for database in paths.databases
            for suffix in ("", "-wal", "-shm", "-journal")
        )
        exact.update(
            _canonical(str(value) + ".lock") for value in paths.atomic_files
        )
        return (
            exact <= self.exact_files
            and set(paths.owned_directories) <= set(self.owned_directories)
            and set(paths.owner_lock_directories) <= set(self.owner_lock_directories)
        )

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


def _canonical_env_override(
    raw: str,
    variable: str,
    *,
    base: Path | None = None,
    absolute: bool = False,
    expand_user: bool = True,
) -> Path:
    """Validate an env-supplied path override lexically before any FS/SMB call."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise PermissionError(
            f"refusing unsafe {variable} path (control character in path)"
        )
    text = raw.strip()
    try:
        cleaned = runtime_paths._strip_extended_or_refuse_prefixed(text)
    except ValueError:
        raise PermissionError(
            f"refusing unsafe {variable} path (UNC or device/NT namespace)"
        ) from None
    if expand_user:
        try:
            expanded = Path(cleaned).expanduser()
        except RuntimeError:
            raise PermissionError(f"refusing unsafe {variable} path") from None
        expanded_text = os.fspath(expanded)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in expanded_text):
            raise PermissionError(
                f"refusing unsafe {variable} path (control character in path)"
            )
        try:
            cleaned_expanded = runtime_paths._strip_extended_or_refuse_prefixed(expanded_text)
        except ValueError:
            raise PermissionError(
                f"refusing unsafe {variable} path (UNC or device/NT namespace)"
            ) from None
        candidate = Path(cleaned_expanded)
    else:
        candidate = Path(cleaned)
    if base is not None:
        if not candidate.is_absolute():
            candidate = base / candidate
    elif absolute and not candidate.is_absolute():
        candidate = candidate.absolute()
    resolved = _canonical(candidate)
    try:
        runtime_paths._strip_extended_or_refuse_prefixed(os.fspath(resolved))
    except ValueError:
        raise PermissionError(
            f"refusing unsafe {variable} path (resolved to UNC or device/NT namespace)"
        ) from None
    return resolved


def live_control_plane_inventory(*, additional=None) -> ControlPlaneInventory:
    """Read actual state-path precedence without state_path's mkdir/migration IO.

    additional is a trusted live callback returning ControlPlanePaths for stores
    constructed with explicit paths (e.g. terminal output), not request fields.
    """
    configured = runtime_paths._configured_home()
    raw_home = runtime_paths.default_home()
    parsed_home = runtime_paths.try_parse_local_root(os.fspath(raw_home), expand_user=True)
    if parsed_home is None:
        raise PermissionError(
            "refusing unsafe SONDER_HOME/SONDER_STATE_HOME path "
            "(UNC, device/NT, relative, or drive-root spelling)"
        )
    home = _canonical(parsed_home)
    if runtime_paths.try_parse_local_root(os.fspath(home), expand_user=False) is None:
        raise PermissionError(
            "refusing unsafe SONDER_HOME/SONDER_STATE_HOME path "
            "(resolved to UNC, device/NT, relative, or drive-root spelling)"
        )

    def state(name, variable=""):
        if configured is not None:
            return _canonical(home / name)
        raw_override = os.environ.get(variable, "") if variable else ""
        if raw_override.strip():
            return _canonical_env_override(raw_override, variable)
        return _canonical(home / name)

    memory_override = os.environ.get("SONDER_DB", "")
    sessions_override = os.environ.get("SONDER_SESSIONS_DB", "")
    fleet = state("fleet.db", "SONDER_FLEET_DB")
    databases = [
        fleet,
        (
            _canonical_env_override(memory_override, "SONDER_DB")
            if memory_override.strip()
            else home / "memory.db"
        ),
        (
            _canonical_env_override(
                sessions_override, "SONDER_SESSIONS_DB", absolute=True, expand_user=False
            )
            if sessions_override.strip()
            else state("sessions.db", "SONDER_SESSIONS_DB")
        ),
        *(state(name, variable) for name, variable in STATE_DATABASES),
    ]
    # app's task repository explicitly selects STATE_HOME/memory.db before the
    # canonical memory resolver. Both are actual composed paths.
    state_home = os.environ.get("SONDER_STATE_HOME", "")
    if state_home.strip():
        parsed_state_home = runtime_paths.try_parse_local_root(state_home, expand_user=True)
        if parsed_state_home is None:
            raise PermissionError(
                "refusing unsafe SONDER_STATE_HOME path "
                "(UNC, device/NT, relative, or drive-root spelling)"
            )
        resolved_state_home = _canonical(parsed_state_home)
        if (
            runtime_paths.try_parse_local_root(
                os.fspath(resolved_state_home), expand_user=False
            )
            is None
        ):
            raise PermissionError(
                "refusing unsafe SONDER_STATE_HOME path "
                "(resolved to UNC, device/NT, relative, or drive-root spelling)"
            )
        databases.append(_canonical(resolved_state_home / "memory.db"))
    files = [state("fleet-principal.json", "SONDER_FLEET_PRINCIPAL_FILE")]
    workspace = Path(__file__).resolve().parents[3]
    roots_override = os.environ.get("SONDER_FILE_ROOTS_FILE", "")
    files.extend(
        (
            (
                _canonical_env_override(roots_override, "SONDER_FILE_ROOTS_FILE")
                if roots_override.strip()
                else home / "file_roots.local"
            ),
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
        raw = os.environ.get(variable, "")
        files.append(
            _canonical_env_override(raw, variable, base=workspace)
            if raw.strip()
            else _canonical(workspace / name)
        )
    policy_override = os.environ.get("SONDER_RUNTIME_POLICY", "")
    policy = (
        _canonical_env_override(policy_override, "SONDER_RUNTIME_POLICY")
        if policy_override.strip()
        else state("runtime_policy.json")
    )
    rotation_override = os.environ.get("SONDER_ROTATION_STATE", "")
    rotation = (
        _canonical_env_override(rotation_override, "SONDER_ROTATION_STATE")
        if rotation_override.strip()
        else home / "secrets" / "rotation.json"
    )
    files.append(state("model-request-admission.sqlite3.lock"))
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
        override = os.environ.get(variable, "")
        atomic.append(
            _canonical_env_override(
                override, variable, absolute=True, expand_user=False
            )
            if override.strip()
            else home / name
        )
    atomic.append(home / "workflows.json")
    # The persisted permission mode (permission_modes._state_path). Written
    # atomically through a ``.tmp-`` sibling, which ``protects`` also covers.
    atomic.append(home / "permission_mode.json")
    # Live emotion-vector tuning is saved beside it (the bundled default in
    # the checkout is covered by the workspace entry above).
    atomic.append(home / "emotion_vectors.json")
    # The operator's live system profile (system_profile.live_path), injected
    # into every system prompt; the bundled default is covered by the workspace
    # entry above.
    atomic.append(home / "system_profile.md")
    # The operator's model tool policy overlay (model_tool_policy.overlay_path:
    # the env override wins over the state home). Only the attended console
    # editor writes it, under file_lock and through a ``.tmp-`` sibling; a model
    # write would grant tools or drop the operator's deny profiles.
    from ..model_tool_policy import OVERLAY_ENV, OVERLAY_FILE_NAME

    tool_policy_override = os.environ.get(OVERLAY_ENV, "")
    atomic.append(
        _canonical_env_override(tool_policy_override, OVERLAY_ENV)
        if tool_policy_override.strip()
        else state(OVERLAY_FILE_NAME)
    )
    from ..model_lists import OVERLAY_ENV as lists_env, OVERLAY_FILE_NAME as lists_file

    lists_override = os.environ.get(lists_env, "")
    atomic.append(
        _canonical_env_override(lists_override, lists_env)
        if lists_override.strip()
        else state(lists_file)
    )
    from .unsafe_lab import AUDIT_PATH_ENV as unsafe_lab_audit_env, _audit_path

    catalog = os.environ.get("SONDER_LANE_TEST_TARGETS_FILE", "")
    if catalog.strip():
        files.append(_canonical_env_override(catalog, "SONDER_LANE_TEST_TARGETS_FILE"))
    unsafe_lab_override = os.environ.get(unsafe_lab_audit_env, "")
    unsafe_lab_audit = (
        _canonical_env_override(unsafe_lab_override, unsafe_lab_audit_env)
        if unsafe_lab_override.strip()
        else _canonical_env_override(
            os.fspath(_audit_path(os.environ)), unsafe_lab_audit_env
        )
    )
    audits = [
        state(os.path.join("audit", "tool-receipts.jsonl"), "SONDER_TOOL_AUDIT"),
        unsafe_lab_audit,
    ]
    owned, lock_dirs = [
        home / "secrets",
        home / "terminal-output",
        home / "locks",
        # Approved playbook entries are injected into the system prompt, so a
        # model write here would forge owner-approved instructions.
        home / "playbooks",
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
