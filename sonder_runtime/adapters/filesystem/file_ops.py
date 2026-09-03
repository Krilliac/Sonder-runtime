"""Guarded filesystem operations for Sonder tools.

Default policy is intentionally conservative: read/write/delete are limited to
approved roots, file sizes are bounded, and deletes dry-run unless explicitly
confirmed. Broader system access requires an explicit bypass decision by the
server layer.
"""
from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import hashlib
import json
import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath

import sonder_runtime.adapters.git_discovery as git_discovery
from sonder_runtime.platform import paths as runtime_paths

# Preserve the packaged filesystem adapter's historical attribute shape while
# callers migrate from the old root ``sonder_paths`` name.  This is an alias
# to the canonical platform module, not a second path implementation.
sonder_paths = runtime_paths


MAX_READ_BYTES = 256_000
MAX_WRITE_BYTES = 1_000_000
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_BATCH_FILES = 32
MAX_BATCH_BYTES = 4_000_000
MAX_BATCH_SNAPSHOT_BYTES = 4_000_000
MAX_BATCH_JSON_BYTES = MAX_BATCH_BYTES + 128_000
MAX_FIND_RESULTS = 200
# Ceiling on the pre-write/pre-delete snapshot decode (see _read_text_if_file).
MAX_SNAPSHOT_BYTES = MAX_WRITE_BYTES
SNAPSHOT_CHUNK_BYTES = 1 << 16
DEFAULT_ROOTS_FILE = "file_roots.local"
CONTROL_CONFIG_FILES = {
    "file_roots.local", "permissions.json", "workflows.json",
    "emotion_vectors.json", "system_profile.md",
}
SECRET_FILES = {
    ".credentials.json", ".netrc", ".token", "auth.json",
    "credentials.json", "secrets.json", "token.json",
}
SECRET_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
SENSITIVE_READ_DIRECTORIES = {".git", ".ssh", ".aws", ".azure", ".kube"}
# Sonder's own first-party package below the install root. The mutation guard
# used to recognize Sonder modules only by ``parent == root``, which was true
# when every module sat directly in the install directory. The SPEC-3 Phase 5
# extraction moved live control logic into this package -- permission_rules
# imports ``sonder_runtime.domain.execution.policy`` at module load -- so all
# 46 of its modules were writable with no developer token while the byte-
# identical edit to <root>/server.py was refused. That is unauthenticated code
# execution in the runtime process at the next start, which is exactly what the
# root-level clause exists to prevent.
RUNTIME_PACKAGE_DIRS = ("sonder_runtime",)
_BATCH_WRITE_LOCK = threading.RLock()


class BatchWriteError(RuntimeError):
    """A rejected or rolled-back multi-file write with structured details."""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines()) or 1


def _read_text_if_file(path: Path) -> str | None:
    """Decoded contents of *path*, or ``None`` when it is too large to snapshot.

    This snapshot is line-delta bookkeeping, not data the caller asked for, so
    it must never cost more memory than the operation it annotates. It used to
    decode the whole existing file with no cap at all, on a path reachable with
    no credentials and no destructive intent: ``file_delete`` defaults to
    ``dry_run=True`` and still ran the snapshot before returning, and
    ``workspace_root()`` (which holds a 478 MB CUDA DLL under ``venv/``) is an
    unconditional allowed root. ``errors="replace"`` turns each invalid byte
    into U+FFFD, which forces CPython off its Latin-1 string representation, so
    478 MB of binary decoded to ~956 MB of UCS-2 held alongside the 478 MB
    source -- and ``_line_count`` then split it on every \\n, \\r, \\x0b, \\x0c,
    \\x85, U+2028 and U+2029 in that binary into a list of millions of strs. A
    few concurrent previews were enough to swap or OOM the server. The cap is
    ``MAX_WRITE_BYTES`` rather than something smaller so the equality and
    concatenation the callers do with this value stay exact for every input a
    write could actually produce.
    """
    if not path.exists() or not path.is_file():
        return ""
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _line_count_on_disk(path: Path) -> int:
    """Exact line count for *path* without holding the file in memory.

    Streams the bytes so an oversized file (the case where
    ``_read_text_if_file`` declines to decode) still reports a real count
    instead of a zero that reads like an empty file.
    """
    if not path.exists() or not path.is_file():
        return 0
    count = 0
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(SNAPSHOT_CHUNK_BYTES)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                tail = chunk[-1:]
    except OSError:
        return 0
    # A final line with no terminating newline still counts, matching
    # ``str.splitlines``.
    if tail and tail != b"\n":
        count += 1
    return count


def workspace_root() -> Path:
    """Sonder's own checkout: the base for relative paths with no project scope.

    This module used to live at the repository root, where "the directory of
    this file" *was* the checkout. The strangler migration moved it four
    levels down and the expression silently started naming
    ``sonder_runtime/adapters/filesystem`` -- measured 2026-09-03, when an
    agent's ``ledger/core.py`` resolved to a path inside the adapter package.
    The directory that contains the ``sonder_runtime`` package is the
    checkout (or the installed payload), so name it explicitly.
    """
    return Path(__file__).resolve().parents[3]


def inside_allowed_roots(path, extra_roots: str = "") -> bool:
    """Whether ``path`` lies inside a root the deployment already exposes."""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return any(_is_inside(resolved, root) for root in allowed_roots(extra_roots))


def _split_roots(raw: str) -> list[Path]:
    roots = []
    for item in (raw or "").split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser())
    return roots


def roots_file_path() -> Path:
    configured = os.environ.get("SONDER_FILE_ROOTS_FILE", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(runtime_paths.default_home()) / DEFAULT_ROOTS_FILE
    )


def _roots_from_file() -> list[Path]:
    try:
        lines = roots_file_path().read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    roots = []
    for line in lines:
        value = line.strip()
        if value and not value.startswith("#"):
            roots.append(Path(value).expanduser())
    return roots


# Reach granted to the call in flight on this context: providers that answer
# with the extra roots a one-shot approval covered (see ``reach_scope``).
# Every root resolution goes through ``allowed_roots``, so a provider's roots
# are honoured everywhere and containment is still checked against them; no
# provider ever switches the check off the way ``bypass`` does.
_CALL_REACH: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "sonder_call_reach", default=(),
)


@contextlib.contextmanager
def reach_scope(provider):
    """Honour ``provider()``'s roots for the calls inside the body.

    ``provider`` is called at resolution time, not at entry, so a surface can
    install the scope around a whole call -- gate and handler -- and the
    roots appear only once the gate has actually spent an approval for the
    call. Installed by the surfaces that decide a call and by the native MCP
    surface; nothing a caller sends can install one.
    """
    token = _CALL_REACH.set(_CALL_REACH.get() + (provider,))
    try:
        yield
    finally:
        _CALL_REACH.reset(token)


def _reach_roots() -> list[Path]:
    roots: list[Path] = []
    for provider in _CALL_REACH.get():
        try:
            granted = provider()
        except Exception:
            continue
        roots.extend(_split_roots(granted if isinstance(granted, str) else ""))
    return roots


def allowed_roots(extra_roots: str = "") -> list[Path]:
    roots = [workspace_root(), Path(runtime_paths.default_home())]
    roots.extend(_split_roots(os.environ.get("SONDER_FILE_ROOTS", "")))
    roots.extend(_roots_from_file())
    roots.extend(_split_roots(extra_roots))
    roots.extend(_reach_roots())
    out = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = _normalized_absolute(root)
        if resolved not in out:
            out.append(resolved)
    return out


def bypass_enabled() -> bool:
    return os.environ.get("SONDER_FILE_BYPASS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _normalized_absolute(path: Path) -> Path:
    """Absolute form of *path* with ``.``/``..`` collapsed lexically.

    This is the fallback every ``resolve()`` call in this module degrades to,
    and it exists because a bare ``Path.absolute()`` fallback makes containment
    *more permissive* than the ``resolve()`` it replaces. ``absolute()`` does
    not normalize ``..``, and ``_is_inside`` compares raw strings through
    ``os.path.commonpath``, which treats ``..`` as an ordinary path component --
    so ``<root>/../../../Windows/System32/config/SAM`` reads as *inside*
    ``<root>`` when ``resolve()`` would have placed it firmly outside.

    Lexical collapse is not symlink-aware, but on this branch ``resolve()`` has
    already failed so no symlink information is available at all. Collapsing can
    only move a path OUT of a root (``a/link/../..`` shortens), never into one,
    so the degraded branch is never weaker than the normal one. ``normpath``
    also clamps ``..`` at a drive or UNC share root exactly as ``resolve()``
    does, so the two agree on the escaping cases that matter here.
    """
    return Path(os.path.normpath(path.absolute()))


def _resolve_best_effort(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return _normalized_absolute(path.expanduser())


def _workspace_config_path(env_name: str, default_name: str) -> Path:
    raw = os.environ.get(env_name, "").strip()
    path = Path(raw).expanduser() if raw else workspace_root() / default_name
    if not path.is_absolute():
        path = workspace_root() / path
    return _resolve_best_effort(path)


def _control_plane_paths() -> set[Path]:
    root = _resolve_best_effort(workspace_root())
    home = _resolve_best_effort(Path(runtime_paths.default_home()))
    paths = {
        _resolve_best_effort(roots_file_path()),
        root / DEFAULT_ROOTS_FILE,
        root / "permissions.json",
        home / "permissions.json",
        _workspace_config_path("SONDER_WORKFLOWS", "workflows.json"),
        _workspace_config_path("SONDER_EMOTION_VECTORS", "emotion_vectors.json"),
        _workspace_config_path("SONDER_SYSTEM_PROFILE", "system_profile.md"),
    }
    db_override = os.environ.get("SONDER_DB", "").strip()
    if db_override:
        db = Path(db_override).expanduser()
        if not db.is_absolute():
            db = Path.cwd() / db
        db = _resolve_best_effort(db)
    else:
        db = home / "memory.db"
    paths.update({
        db, Path(str(db) + "-wal"), Path(str(db) + "-shm"), Path(str(db) + "-journal"),
        root / "memory.db", root / "memory.db-wal", root / "memory.db-shm", root / "memory.db-journal",
    })
    # Fanout receipts are executable runtime state: even a custom file name
    # must not turn them into an ordinary SQLite target.  Keep the override
    # resolution identical to SONDER_DB above so a relative override is also
    # protected at the process' actual working directory.
    fanout_override = os.environ.get("SONDER_FANOUT_DB", "").strip()
    if fanout_override:
        fanout_db = Path(fanout_override).expanduser()
        if not fanout_db.is_absolute():
            fanout_db = Path.cwd() / fanout_db
        fanout_db = _resolve_best_effort(fanout_db)
    else:
        fanout_db = home / "fanout.db"
    paths.update({
        fanout_db, Path(str(fanout_db) + "-wal"), Path(str(fanout_db) + "-shm"), Path(str(fanout_db) + "-journal"),
        root / "fanout.db", root / "fanout.db-wal", root / "fanout.db-shm", root / "fanout.db-journal",
    })
    return {_resolve_best_effort(path) for path in paths}


def _is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in SECRET_FILES or suffix in SECRET_SUFFIXES:
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    return (
        suffix in {".cfg", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
        and ("credential" in name or "secret" in name)
    )


def _is_sensitive_control_path(path: Path) -> bool:
    path = _resolve_best_effort(path)
    name = path.name.lower()
    return (
        path in _control_plane_paths()
        or name in CONTROL_CONFIG_FILES
        or name in {
            "memory.db", "memory.db-wal", "memory.db-shm",
            # Durable fanout receipts contain sealed execution state.  They
            # are runtime control state, never user data for sqlite_mutate.
            "fanout.db", "fanout.db-wal", "fanout.db-shm",
        }
        or _is_secret_path(path)
    )


def _is_protected_mutation_path(path: Path) -> bool:
    path = _resolve_best_effort(path)
    if _is_sensitive_control_path(path):
        return True
    if path.suffix.lower() != ".py":
        return False
    root = _resolve_best_effort(workspace_root())
    # Sonder's own modules: directly in the install root, or anywhere inside
    # its first-party package (see RUNTIME_PACKAGE_DIRS). A *nested user
    # project* under the root stays editable -- only the package Sonder itself
    # imports is added, so this closes the hole without widening the guard over
    # ordinary workspace code.
    if path.parent == root:
        return True
    return any(_is_inside(path, root / name) for name in RUNTIME_PACKAGE_DIRS)


def _require_mutation_access(path: Path, developer_authorized: bool) -> None:
    if _is_protected_mutation_path(path) and not developer_authorized:
        # The path goes before the phrase: "token: <path>" reads as a
        # credential to the output redactor, and this message is shown and
        # audited through it on every surface.
        raise PermissionError(
            "refusing to mutate protected Sonder control-plane path %s "
            "without an authenticated developer token" % path
        )


def _is_personal_corpus(path: Path) -> bool:
    """True for the local personal chat/training corpus.

    ``combined_personal.jsonl`` (and a ``sonder-personal-lora`` adapter tree)
    hold raw personal conversation/training data. They are not part of the
    control-plane classifier that guards mutations, but for *reads* they are
    first-class secrets: relaying their contents out is a confidentiality
    breach, so the direct read tools must gate them like any other secret.
    """
    if path.name.lower() == "combined_personal.jsonl":
        return True
    return any(part.lower() == "sonder-personal-lora" for part in path.parts)


def _is_protected_read_path(path: Path) -> bool:
    """Paths whose CONTENTS are secret/control state and must not be read.

    Mirrors the mutation classifier (``_is_protected_mutation_path`` --
    secrets, control-plane config/state, and root-level Sonder modules) and
    adds the personal corpus, which is read-sensitive even though writing it
    is not itself control-plane-protected. Writes already refuse the mutation
    set; reads previously refused none of it -- this closes that asymmetry.
    """
    path = _resolve_best_effort(path)
    return _is_protected_mutation_path(path) or _is_personal_corpus(path)


def _require_read_access(path: Path, authorized: bool) -> None:
    """Refuse a non-authorized read of a secret/control-plane path.

    ``authorized`` is the developer-token OR bypass signal (mirroring the
    escape hatch the write guard honors for a developer token). Fails closed
    with a clear error; an unclassified workspace file is never affected.
    """
    if _is_protected_read_path(path) and not authorized:
        raise PermissionError(
            "refusing to read protected Sonder secret/control-plane path %s "
            "without an authenticated developer token or bypass" % path
        )


def require_read_access(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> Path:
    """Resolve *path* and enforce the secret/control-plane read guard.

    Public entry point for read tools whose bodies live outside this module
    (line-range and image inspection in ``workbench``) so they get the same
    fail-closed guard as ``read_file`` / ``inspect_data``. Returns the
    resolved path when the read is permitted.
    """
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_read_access(p, developer_authorized or bypass)
    return p


def _is_inside(path: Path, root: Path) -> bool:
    """True when *path* is contained in *root*.

    Both operands are normalized first. ``os.path.commonpath`` is purely
    lexical and treats ``..`` as an ordinary component, so an un-normalized
    argument makes containment answer YES for a path that escapes -- see
    ``_normalized_absolute``. Callers normally pass resolved paths (for which
    ``normpath`` is a no-op), but this is the single primitive every containment
    decision in the module funnels through, so it must not depend on every
    caller having normalized correctly.

    ``normcase`` is part of that normalization on Windows, where the filesystem
    is case-insensitive but ``commonpath`` is not: ``commonpath`` of
    ``C:\\WORK\\sub`` and ``C:\\work`` returns ``C:\\WORK``, which does not equal
    the root, so a genuinely contained path was refused with no explanation.
    It fails closed, so it never allowed an escape -- but a guard that denies
    legitimate reads based on how the caller happened to spell a drive letter is
    still wrong. On POSIX ``normcase`` is the identity, so nothing changes there.
    """
    path_text = os.path.normcase(os.path.normpath(str(path)))
    root_text = os.path.normcase(os.path.normpath(str(root)))
    try:
        return os.path.commonpath([path_text, root_text]) == root_text
    except ValueError:
        return False


def _foreign_absolute(raw: str) -> bool:
    """True when *raw* is absolute in a path flavor the host cannot resolve.

    A Windows drive/UNC path (``C:\\x``, ``\\\\server\\share``) on POSIX -- or a
    POSIX-rooted path on Windows -- is absolute in a syntax the native ``Path``
    does not understand, so ``Path()`` silently treats it as a *relative* name
    and rebases it under the workspace, defeating every containment check. We
    must recognize these escaping forms independent of host OS and reject them
    outright rather than let them masquerade as workspace-relative reads.
    """
    windows = PureWindowsPath(raw)
    windows_absolute = bool(windows.drive or windows.anchor)
    posix_absolute = PurePosixPath(raw).is_absolute()
    native_absolute = Path(raw).is_absolute()
    # Absolute in some flavor, yet the native OS disagrees => foreign/escaping.
    return (windows_absolute or posix_absolute) and not native_absolute


def resolve_repository_read_path(
    path: str,
    *,
    allow_workspace_root: bool = False,
    reject_sensitive: bool = True,
    extra_roots: str = "",
) -> Path:
    """Resolve an agent read path inside an AUTHORIZED root.

    A relative path resolves against the workspace root, as before. An absolute
    path is accepted only when it lands inside one of the roots the operator
    already authorized for the guarded file tools (``allowed_roots()`` --
    i.e. ``file_roots.local`` / ``SONDER_FILE_ROOTS``).

    Previously ANY absolute path was rejected outright ("must be relative") and
    the only root was Sonder's own install directory, so a repository-scoped
    agent could never read the repository it was pointed at -- while the failure
    message told the operator to authorize it in ``file_roots.local``, which
    this resolver never consulted. That made the whole delegated repository lane
    unusable on any external repo. Authorized roots are now honored here too;
    every other guard (no escaping a root, no secrets, no .git/.ssh/control
    state) is unchanged, so this grants nothing the operator has not already
    granted the direct file tools.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("repository path must be a non-empty path")
    raw = path.strip()
    if _foreign_absolute(raw):
        # e.g. a Windows drive/UNC path reaching a POSIX host: Path() would
        # rebase it under the workspace and pass containment. Reject it as an
        # escaping absolute regardless of which OS we are running on.
        raise PermissionError(
            "repository path uses a non-native absolute form "
            "(Windows drive/UNC or foreign root) and escapes the workspace: %s"
            % raw
        )
    candidate = Path(raw)
    expanded = candidate.expanduser()
    windows_candidate = PureWindowsPath(raw)
    is_absolute = bool(
        candidate.is_absolute() or expanded.is_absolute()
        or candidate.drive or candidate.anchor
        or windows_candidate.is_absolute() or windows_candidate.drive
    )

    workspace = _resolve_best_effort(workspace_root())
    if is_absolute:
        resolved = _resolve_best_effort(expanded)
        roots = [_resolve_best_effort(root) for root in allowed_roots(extra_roots)]
        root = next((r for r in roots if _is_inside(resolved, r) or resolved == r), None)
        if root is None:
            raise PermissionError(
                "repository path is outside every authorized root; add it to "
                "file_roots.local or pass a path relative to the workspace"
            )
    else:
        root = workspace
        resolved = _resolve_best_effort(root / candidate)
        if not _is_inside(resolved, root) and resolved != root:
            raise PermissionError("repository path escapes the workspace root")

    if resolved == root and not allow_workspace_root:
        raise PermissionError("repository file path must resolve below its root")
    if reject_sensitive:
        relative = resolved.relative_to(root) if resolved != root else Path(".")
        if (
            _is_sensitive_control_path(resolved)
            or any(part.lower() in SENSITIVE_READ_DIRECTORIES for part in relative.parts)
        ):
            raise PermissionError("repository path is secret or control state")
    return resolved


def _requested_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root() / candidate
    return candidate.absolute()


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PermissionError("could not safely inspect path metadata: %s" % path) from exc
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _require_no_reparse_components(path: Path) -> None:
    current = path
    while True:
        if _is_reparse_point(current):
            raise PermissionError(
                "refusing file operation through a symlink or junction: %s"
                % current
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _require_safe_recursive_delete(
    requested: Path,
    resolved: Path,
    *,
    extra_roots: str,
    bypass: bool,
    developer_authorized: bool,
) -> None:
    if developer_authorized:
        return
    _require_no_reparse_components(requested)
    configured_roots = allowed_roots(extra_roots if bypass else "")
    for configured in configured_roots:
        configured = _resolve_best_effort(configured)
        if resolved == configured or _is_inside(configured, resolved):
            raise PermissionError(
                "refusing recursive deletion of an allowed/configured root without "
                "an authenticated developer token: %s" % configured
            )
    if not resolved.exists() or not resolved.is_dir():
        return
    pending = [resolved]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise PermissionError(
                "could not safely inspect recursive delete tree: %s" % current
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse_point(child):
                    raise PermissionError(
                        "refusing recursive deletion of a tree containing a symlink "
                        "or junction (%s) without an authenticated developer token" % child
                    )
                if _is_sensitive_control_path(child) or _is_protected_mutation_path(child):
                    raise PermissionError(
                        "refusing recursive deletion of a tree containing protected "
                        "control state (%s) without an authenticated developer token" % child
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
            except PermissionError:
                raise
            except OSError as exc:
                raise PermissionError(
                    "could not safely inspect recursive delete entry: %s" % child
                ) from exc


def _delete_tree_guarded(path: Path) -> None:
    """Delete a preflighted tree without traversing reparse points."""
    if _is_reparse_point(path):
        raise PermissionError("refusing to traverse symlink or junction: %s" % path)
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise PermissionError("could not safely traverse delete tree: %s" % path) from exc
    for entry in entries:
        child = Path(entry.path)
        try:
            if entry.is_symlink() or _is_reparse_point(child):
                raise PermissionError("refusing to traverse symlink or junction: %s" % child)
            if _is_sensitive_control_path(child) or _is_protected_mutation_path(child):
                raise PermissionError("refusing to delete protected control state: %s" % child)
            if entry.is_dir(follow_symlinks=False):
                _delete_tree_guarded(child)
                child.rmdir()
            else:
                child.unlink()
        except PermissionError:
            raise
        except OSError as exc:
            raise PermissionError("could not safely delete tree entry: %s" % child) from exc


def resolve_path(path: str, *, extra_roots: str = "", bypass: bool = False) -> Path:
    if not (path or "").strip():
        raise ValueError("empty path")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root() / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = _normalized_absolute(candidate)
    roots = allowed_roots(extra_roots if bypass else "")
    if bypass:
        return resolved
    if any(_is_inside(resolved, root) for root in roots):
        return resolved
    raise PermissionError(
        "path is outside allowed roots. Set SONDER_FILE_ROOTS or use an approved admin/dev bypass."
    )


def resolve_mutation_path(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
) -> Path:
    """Resolve a mutation target without accepting a reparse-point spelling.

    ``resolve_path`` deliberately follows links so read-only callers get the
    canonical path for containment checks.  That is not an acceptable default
    for a mutation: ``file_delete('alias')`` used to resolve a workspace link
    and remove its target, while a write/edit could silently modify the link
    target.  Reject the lexical path whenever it contains a symlink or Windows
    junction, then resolve and recheck the canonical result.  Transfer and
    batch paths already do this; this shared entry point gives single-file
    mutations the same rule.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("empty path")
    if _foreign_absolute(path.strip()):
        raise PermissionError("path uses a foreign absolute path form")
    requested = _requested_path(path)
    _require_no_reparse_components(requested)
    resolved = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_no_reparse_components(resolved)
    return resolved


def policy_text(*, bypass: bool = False, extra_roots: str = "") -> str:
    lines = [
        "filesystem policy",
        "  mode: %s" % ("bypass" if bypass else "guarded"),
        "  delete default: dry-run unless confirm matches DELETE <path>",
        "  max read bytes: %d" % MAX_READ_BYTES,
        "  max write bytes: %d" % MAX_WRITE_BYTES,
        "  max transfer bytes: %d" % MAX_TRANSFER_BYTES,
        "  roots:",
    ]
    for root in allowed_roots(extra_roots if bypass else ""):
        lines.append("    - %s" % root)
    lines.extend([
        "  hot roots file: %s" % roots_file_path(),
        "  env bypass: SONDER_FILE_BYPASS=1",
        "  one call beyond the roots: /approve <call id> (the approved call's extra_roots are honoured once)",
        "  env extra roots: SONDER_FILE_ROOTS=<path%spath>" % os.pathsep,
    ])
    return "\n".join(lines)


def find_files(
    query: str = "*",
    root: str = "",
    *,
    max_results: int = 50,
    extra_roots: str = "",
    bypass: bool = False,
    include_ignored: bool = False,
) -> dict:
    root_path = resolve_path(root or ".", extra_roots=extra_roots, bypass=bypass)
    if not root_path.exists():
        raise FileNotFoundError("search root not found: %s" % root_path)
    if not root_path.is_dir():
        raise ValueError("root is not a directory: %s" % root_path)
    pattern = (query or "*").strip() or "*"
    limit = max(1, min(MAX_FIND_RESULTS, int(max_results or 50)))
    results = []
    enforce_git_visibility = not include_ignored
    visible = (
        git_discovery.visible_paths(root_path)
        if enforce_git_visibility else None
    )

    def finish(truncated: bool) -> dict:
        if enforce_git_visibility:
            git_discovery.require_unchanged(root_path, visible)
        return {
            "root": str(root_path), "query": pattern, "results": results,
            "truncated": truncated, "limit": limit,
        }

    for base, dirs, files in os.walk(root_path):
        base_path = Path(base)
        dirs[:] = [
            name for name in dirs
            if name not in {".git", ".pytest_cache", "venv", "__pycache__"}
            and not _is_reparse_point(base_path / name)
            and (
                visible is None
                or os.path.normcase(
                    str((base_path / name).relative_to(root_path))
                ) in visible
            )
        ]
        names = dirs + files
        for name in names:
            path = Path(base) / name
            rel = str(path.relative_to(root_path))
            if visible is not None and os.path.normcase(rel) not in visible:
                continue
            if _is_reparse_point(path):
                continue
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern) or pattern.lower() in rel.lower():
                results.append({
                    "path": str(path),
                    "relative": rel,
                    "type": "dir" if path.is_dir() else "file",
                    "bytes": path.stat().st_size if path.is_file() else 0,
                })
                if len(results) >= limit:
                    # Hit the cap with the walk unfinished: more may match.
                    # Signal it so counting callers don't silently undercount.
                    return finish(True)
    return finish(False)


def read_file(
    path: str,
    *,
    max_bytes: int = MAX_READ_BYTES,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_read_access(p, developer_authorized or bypass)
    if not p.exists():
        raise FileNotFoundError("file not found: %s" % p)
    if not p.is_file():
        raise ValueError("path is not a file: %s" % p)
    size = p.stat().st_size
    limit = max(1, min(MAX_READ_BYTES, int(max_bytes or MAX_READ_BYTES)))
    data = p.read_bytes()[:limit]
    text = data.decode("utf-8", errors="replace")
    return {"path": str(p), "bytes": size, "truncated": size > limit, "text": text}


def make_directory(
    path: str,
    *,
    parents: bool = True,
    exist_ok: bool = True,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    p = resolve_mutation_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    existed = p.exists()
    if existed and not p.is_dir():
        raise FileExistsError("directory path is an existing file: %s" % p)
    p.mkdir(parents=bool(parents), exist_ok=bool(exist_ok))
    return {
        "path": str(p),
        "action": "directory_exists" if existed else "create_directory",
        "created": not existed,
        "parents": bool(parents),
        "bytes": 0,
        "lines_added": 0,
        "lines_edited": 0,
        "lines_deleted": 0,
    }


def _resolve_transfer_path(
    raw_path: str,
    *,
    label: str,
    extra_roots: str,
    bypass: bool,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise ValueError("%s must be an explicit non-empty file path" % label)
    raw_path = raw_path.strip()
    if _foreign_absolute(raw_path):
        raise PermissionError("%s uses a foreign absolute path form" % label)
    requested = _requested_path(raw_path)
    _require_no_reparse_components(requested)
    resolved = resolve_path(raw_path, extra_roots=extra_roots, bypass=bypass)
    _require_no_reparse_components(resolved)
    roots = allowed_roots(extra_roots if bypass else "")
    root = next(
        (
            _resolve_best_effort(candidate)
            for candidate in roots
            if resolved == _resolve_best_effort(candidate)
            or _is_inside(resolved, _resolve_best_effort(candidate))
        ),
        None,
    )
    if root is None:
        raise PermissionError("%s is outside every allowed root" % label)
    relative = resolved.relative_to(root) if resolved != root else Path(".")
    sensitive_component = next(
        (
            part
            for part in relative.parts
            if part.lower() in SENSITIVE_READ_DIRECTORIES
        ),
        None,
    )
    if sensitive_component is not None:
        raise PermissionError(
            "%s may not access sensitive directory %s" % (label, sensitive_component)
        )
    if _is_sensitive_control_path(resolved):
        raise PermissionError("%s is sensitive Sonder control state" % label)
    return resolved


def _stat_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _stat_identity(left) == _stat_identity(right)


class _DirectoryAnchor:
    """Keep an allowed directory anchored while names below it are changed.

    POSIX operations use descriptor-relative syscalls. Windows' stdlib exposes
    no dir-fd operations, so the fallback verifies the directory file identity
    immediately before and after each path operation and fails closed if the
    name was rebound. Holding source file descriptors still anchors all bytes
    read on every platform.
    """

    def __init__(self, path: Path, expected: os.stat_result):
        self.path = path
        self.expected = expected
        self.fd: int | None = None
        self._windows_handle = None

    def __enter__(self):
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create_file.restype = ctypes.c_void_p
            handle = create_file(
                str(self.path),
                0,
                0x00000001 | 0x00000002,  # share read/write, never delete
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self._windows_handle = (kernel32, handle)
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                fd = os.open(self.path, flags)
            except PermissionError:
                if os.name != "nt":
                    raise
            else:
                actual = os.fstat(fd)
                if not _same_identity(actual, self.expected):
                    os.close(fd)
                    raise PermissionError(
                        "validated transfer directory changed before use"
                    )
                self.fd = fd
            self.validate()
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._windows_handle is not None:
            kernel32, handle = self._windows_handle
            kernel32.CloseHandle(handle)
            self._windows_handle = None

    @property
    def descriptor_relative(self) -> bool:
        required = (os.open, os.unlink, os.link, os.replace)
        return self.fd is not None and all(
            operation in os.supports_dir_fd for operation in required
        )

    def validate(self) -> None:
        _require_no_reparse_components(self.path)
        current = self.path.lstat()
        if not stat.S_ISDIR(current.st_mode) or not _same_identity(current, self.expected):
            raise PermissionError("validated transfer directory changed during operation")
        if self.fd is not None and not _same_identity(os.fstat(self.fd), current):
            raise PermissionError("validated transfer directory handle no longer matches path")

    def create_temp(self, destination_name: str) -> tuple[int, str, Path]:
        prefix = ".%s.sonder-transfer-" % destination_name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        if self.descriptor_relative:
            for _ in range(100):
                name = prefix + secrets.token_hex(8)
                try:
                    fd = os.open(name, flags, 0o600, dir_fd=self.fd)
                except FileExistsError:
                    continue
                try:
                    self.validate()
                except Exception:
                    os.close(fd)
                    os.unlink(name, dir_fd=self.fd)
                    raise
                return fd, name, self.path / name
            raise FileExistsError("could not allocate a unique transfer temporary file")
        self.validate()
        fd, raw_path = tempfile.mkstemp(prefix=prefix, dir=str(self.path))
        path = Path(raw_path)
        try:
            self.validate()
        except Exception:
            os.close(fd)
            try:
                self.validate()
                path.unlink()
            except (OSError, PermissionError):
                pass
            raise
        return fd, path.name, path

    def chmod(self, name: str, mode: int) -> None:
        if self.fd is not None and os.chmod in os.supports_dir_fd:
            os.chmod(name, mode, dir_fd=self.fd)
            self.validate()
            return
        self.validate()
        os.chmod(self.path / name, mode)
        self.validate()

    def unlink(self, name: str) -> None:
        if self.descriptor_relative:
            os.unlink(name, dir_fd=self.fd)
            self.validate()
            return
        self.validate()
        (self.path / name).unlink()
        self.validate()

    def link_name(self, source_name: str, destination_name: str) -> None:
        if self.descriptor_relative:
            os.link(
                source_name, destination_name,
                src_dir_fd=self.fd, dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
            self.validate()
            return
        self.validate()
        os.link(
            self.path / source_name,
            self.path / destination_name,
            follow_symlinks=False,
        )
        self.validate()

    def replace_name(self, source_name: str, destination_name: str) -> None:
        if self.descriptor_relative:
            os.replace(
                source_name, destination_name,
                src_dir_fd=self.fd, dst_dir_fd=self.fd,
            )
            self.validate()
            return
        self.validate()
        os.replace(self.path / source_name, self.path / destination_name)
        self.validate()

    def publish(self, temp_name: str, destination_name: str, *, overwrite: bool) -> None:
        if self.descriptor_relative:
            if overwrite:
                os.replace(
                    temp_name, destination_name,
                    src_dir_fd=self.fd, dst_dir_fd=self.fd,
                )
            else:
                os.link(
                    temp_name, destination_name,
                    src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False,
                )
                os.unlink(temp_name, dir_fd=self.fd)
            self.validate()
            return
        self.validate()
        temp_path = self.path / temp_name
        destination = self.path / destination_name
        if overwrite:
            os.replace(temp_path, destination)
        else:
            # Hard-link publication is atomic and fails with EEXIST. Unlike a
            # closed reservation followed by replace, it never clobbers a name
            # another process installed during the copy.
            os.link(temp_path, destination, follow_symlinks=False)
            temp_path.unlink()
        self.validate()


def _windows_open_source(source: Path) -> int:
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(source),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ; deny writes, deletes, and renames
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_validated_source(
    source: Path,
    expected: os.stat_result,
    anchor: _DirectoryAnchor | None = None,
):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        fd = _windows_open_source(source)
    elif anchor is not None and anchor.fd is not None and os.open in os.supports_dir_fd:
        fd = os.open(source.name, flags, dir_fd=anchor.fd)
    else:
        fd = os.open(source, flags)
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or not _same_identity(actual, expected):
            raise PermissionError("validated transfer source changed before open")
        if actual.st_size > MAX_TRANSFER_BYTES:
            raise ValueError(
                "source exceeds max transfer bytes (%d): %s"
                % (MAX_TRANSFER_BYTES, source)
            )
        return os.fdopen(fd, "rb", closefd=True), actual
    except Exception:
        os.close(fd)
        raise


def _require_source_identity(source: Path, expected: os.stat_result) -> None:
    _require_no_reparse_components(source)
    current = source.lstat()
    if (
        not stat.S_ISREG(current.st_mode)
        or not _same_identity(current, expected)
        or current.st_size != expected.st_size
        or current.st_mtime_ns != expected.st_mtime_ns
    ):
        raise PermissionError("validated transfer source changed during operation")
    if current.st_size > MAX_TRANSFER_BYTES:
        raise ValueError(
            "source changed and exceeds max transfer bytes (%d): %s"
            % (MAX_TRANSFER_BYTES, source)
        )


def _require_open_source_unchanged(
    source_stream,
    source: Path,
    expected: os.stat_result,
    bytes_read: int,
) -> None:
    current = os.fstat(source_stream.fileno())
    if (
        not _same_identity(current, expected)
        or current.st_size != bytes_read
        or current.st_size > MAX_TRANSFER_BYTES
        or current.st_mtime_ns != expected.st_mtime_ns
    ):
        raise PermissionError("validated transfer source changed while it was read")
    _require_source_identity(source, expected)


def _prepare_transfer(
    source: str,
    destination: str,
    *,
    overwrite: bool,
    moving: bool,
    extra_roots: str,
    bypass: bool,
    developer_authorized: bool,
) -> tuple[
    Path, Path, os.stat_result, os.stat_result, os.stat_result,
    os.stat_result | None, bool,
]:
    src = _resolve_transfer_path(
        source, label="source", extra_roots=extra_roots, bypass=bypass,
    )
    dst = _resolve_transfer_path(
        destination, label="destination", extra_roots=extra_roots, bypass=bypass,
    )
    _require_read_access(src, developer_authorized or bypass)
    _require_mutation_access(dst, developer_authorized)
    if moving:
        _require_mutation_access(src, developer_authorized)
    if not src.exists():
        raise FileNotFoundError("source file not found: %s" % src)
    if not src.is_file():
        raise ValueError("source must be a regular file: %s" % src)
    if src == dst:
        raise ValueError("source and destination must be different files")
    source_stat = src.lstat()
    source_parent_stat = src.parent.lstat()
    if source_stat.st_size > MAX_TRANSFER_BYTES:
        raise ValueError(
            "source exceeds max transfer bytes (%d): %s" % (MAX_TRANSFER_BYTES, src)
        )
    if not dst.parent.exists() or not dst.parent.is_dir():
        raise FileNotFoundError("destination parent directory does not exist: %s" % dst.parent)
    _require_no_reparse_components(dst.parent)
    destination_parent_stat = dst.parent.lstat()
    destination_exists = dst.exists()
    destination_stat = dst.lstat() if destination_exists else None
    if destination_exists:
        if not dst.is_file():
            raise ValueError("destination must be a regular file: %s" % dst)
        try:
            if os.path.samefile(src, dst):
                raise ValueError("source and destination identify the same file")
        except OSError:
            pass
        if not overwrite:
            raise FileExistsError(
                "destination exists (set overwrite=true to replace): %s" % dst
            )
    return (
        src, dst, source_stat, source_parent_stat,
        destination_parent_stat, destination_stat, destination_exists,
    )


def _atomic_copy_bytes(
    source_stream,
    source_stat: os.stat_result,
    destination: Path,
    destination_anchor: _DirectoryAnchor,
    *,
    overwrite: bool,
    pre_publish=None,
) -> tuple[str, int]:
    temp_name = None
    digest = hashlib.sha256()
    try:
        temp_fd, temp_name, _temp_path = destination_anchor.create_temp(destination.name)
        source_stream.seek(0)
        with os.fdopen(temp_fd, "wb") as outgoing:
            total = 0
            while True:
                chunk = source_stream.read(SNAPSHOT_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_TRANSFER_BYTES:
                    raise ValueError("source changed and exceeded max transfer bytes")
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        destination_anchor.chmod(temp_name, stat.S_IMODE(source_stat.st_mode))
        if pre_publish is not None:
            pre_publish(total)
        destination_anchor.publish(
            temp_name, destination.name, overwrite=overwrite,
        )
        temp_name = None
        return digest.hexdigest(), total
    finally:
        if temp_name is not None:
            try:
                destination_anchor.unlink(temp_name)
            except (FileNotFoundError, PermissionError):
                pass


def copy_file(
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    (
        src, dst, source_stat, _source_parent_stat,
        destination_parent_stat, _destination_stat, destination_exists,
    ) = _prepare_transfer(
        source,
        destination,
        overwrite=bool(overwrite),
        moving=False,
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    with (
        _DirectoryAnchor(src.parent, _source_parent_stat) as source_anchor,
        _DirectoryAnchor(dst.parent, destination_parent_stat) as destination_anchor,
    ):
        source_stream, opened_stat = _open_validated_source(
            src, source_stat, source_anchor,
        )
        with source_stream:
            digest, total = _atomic_copy_bytes(
                source_stream,
                opened_stat,
                dst,
                destination_anchor,
                overwrite=bool(overwrite),
                pre_publish=lambda bytes_read: _require_open_source_unchanged(
                    source_stream, src, opened_stat, bytes_read,
                ),
            )
    return {
        "action": "copy",
        "bytes": total,
        "destination": str(dst),
        "overwrite": bool(overwrite),
        "path": str(dst),
        "replaced": destination_exists,
        "sha256": digest,
        "source": str(src),
    }


def _move_backup(anchor: _DirectoryAnchor, destination_name: str) -> str:
    prefix = ".%s.sonder-move-backup-" % destination_name
    for _ in range(100):
        backup_name = prefix + secrets.token_hex(8)
        try:
            anchor.link_name(destination_name, backup_name)
        except FileExistsError:
            continue
        return backup_name
    raise FileExistsError("could not allocate a unique move rollback name")


def _rollback_move(
    anchor: _DirectoryAnchor,
    destination_name: str,
    backup_name: str | None,
) -> None:
    try:
        anchor.unlink(destination_name)
    except FileNotFoundError:
        pass
    if backup_name is not None:
        anchor.replace_name(backup_name, destination_name)


def _require_destination_identity(
    destination: Path,
    expected: os.stat_result | None,
) -> None:
    if expected is None:
        return
    _require_no_reparse_components(destination)
    current = destination.lstat()
    if not stat.S_ISREG(current.st_mode) or not _same_identity(current, expected):
        raise PermissionError("validated transfer destination changed during operation")


def move_file(
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    if type(overwrite) is not bool:
        raise ValueError("overwrite must be a boolean")
    (
        src, dst, source_stat, source_parent_stat,
        destination_parent_stat, destination_stat, destination_exists,
    ) = _prepare_transfer(
        source,
        destination,
        overwrite=bool(overwrite),
        moving=True,
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    backup_name = None
    with (
        _DirectoryAnchor(src.parent, source_parent_stat) as source_anchor,
        _DirectoryAnchor(dst.parent, destination_parent_stat) as destination_anchor,
    ):
        published = False
        if destination_exists:
            _require_destination_identity(dst, destination_stat)
            backup_name = _move_backup(destination_anchor, dst.name)
        try:
            source_stream, opened_stat = _open_validated_source(
                src, source_stat, source_anchor,
            )
            with source_stream:
                digest, total = _atomic_copy_bytes(
                    source_stream,
                    opened_stat,
                    dst,
                    destination_anchor,
                    overwrite=destination_exists,
                    pre_publish=lambda bytes_read: (
                        _require_open_source_unchanged(
                            source_stream, src, opened_stat, bytes_read,
                        ),
                        _require_destination_identity(dst, destination_stat),
                    ),
                )
                published = True
                try:
                    _require_open_source_unchanged(
                        source_stream, src, opened_stat, total,
                    )
                except Exception:
                    _rollback_move(destination_anchor, dst.name, backup_name)
                    backup_name = None
                    published = False
                    raise
            try:
                _require_source_identity(src, opened_stat)
                source_anchor.unlink(src.name)
            except Exception:
                _rollback_move(destination_anchor, dst.name, backup_name)
                backup_name = None
                raise
            if backup_name is not None:
                try:
                    destination_anchor.unlink(backup_name)
                except OSError:
                    # Restore both names if even rollback-backup cleanup fails.
                    destination_source_stat = dst.lstat()
                    restore_stream, restore_stat = _open_validated_source(
                        dst, destination_source_stat, destination_anchor,
                    )
                    with restore_stream:
                        _atomic_copy_bytes(
                            restore_stream,
                            restore_stat,
                            src,
                            source_anchor,
                            overwrite=False,
                            pre_publish=lambda bytes_read: _require_open_source_unchanged(
                                restore_stream, dst, restore_stat, bytes_read,
                            ),
                        )
                    _rollback_move(destination_anchor, dst.name, backup_name)
                    backup_name = None
                    raise
                backup_name = None
        except Exception:
            if backup_name is not None and not published:
                try:
                    destination_anchor.unlink(backup_name)
                except OSError:
                    pass
            raise
    return {
        "action": "move",
        "bytes": total,
        "destination": str(dst),
        "method": "copy-delete",
        "overwrite": bool(overwrite),
        "path": str(dst),
        "replaced": destination_exists,
        "sha256": digest,
        "source": str(src),
    }


def write_file(
    path: str,
    content: str,
    *,
    mode: str = "create",
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    p = resolve_mutation_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    before = _read_text_if_file(p)
    before_lines = _line_count(before) if before is not None else _line_count_on_disk(p)
    data = (content or "").encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ValueError("content exceeds max write bytes")
    mode = (mode or "create").lower()
    if mode not in {"create", "overwrite", "append"}:
        raise ValueError("mode must be create, overwrite, or append")
    if mode == "create" and p.exists():
        raise FileExistsError("file exists (use mode=overwrite to replace): %s" % p)
    missing_parents = []
    cursor = p.parent
    while not cursor.exists():
        missing_parents.append(str(cursor))
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with p.open("a", encoding="utf-8", newline="") as f:
            f.write(content or "")
    else:
        p.write_text(content or "", encoding="utf-8", newline="")
    if mode != "append":
        after_lines = _line_count(content or "")
    elif before is not None:
        after_lines = _line_count(before + (content or ""))
    else:
        # Oversized existing file: never concatenate it just to count lines.
        after_lines = before_lines + _line_count(content or "")
    if mode in {"create", "append"}:
        lines_added = _line_count(content or "")
        lines_deleted = 0
        lines_edited = 0
        action = "create" if mode == "create" else "append"
    else:
        lines_added = max(0, after_lines - before_lines)
        lines_deleted = max(0, before_lines - after_lines)
        lines_edited = min(before_lines, after_lines) if before != (content or "") else 0
        action = "overwrite"
    written_bytes = p.stat().st_size if mode == "append" else len(data)
    return {
        "path": str(p),
        "bytes": written_bytes,
        "mode": mode,
        "action": action,
        "lines_before": before_lines,
        "lines_after": after_lines,
        "lines_added": lines_added,
        "lines_edited": lines_edited,
        "lines_deleted": lines_deleted,
        "created_directories": list(reversed(missing_parents)),
    }


def _batch_payload(operations):
    if isinstance(operations, str):
        if (
            len(operations) > MAX_BATCH_JSON_BYTES
            or len(operations.encode("utf-8")) > MAX_BATCH_JSON_BYTES
        ):
            raise ValueError("operations JSON exceeds max batch input bytes")
        try:
            operations = json.loads(operations)
        except (TypeError, ValueError) as exc:
            raise ValueError("operations_json must be valid JSON") from exc
    if not isinstance(operations, list):
        raise ValueError("operations must be a JSON list")
    if not operations:
        raise ValueError("operations list must not be empty")
    if len(operations) > MAX_BATCH_FILES:
        raise ValueError("operations exceeds max file count (%d)" % MAX_BATCH_FILES)
    return operations


def _batch_requested_path(path: str) -> Path:
    """Return the lexical requested path used for reparse-point checks."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root() / candidate
    return candidate.absolute()


def _batch_preflight(
    operations,
    *,
    extra_roots: str,
    bypass: bool,
) -> tuple[list[dict], int]:
    prepared = []
    results = []
    seen = set()
    seen_existing_identities = set()
    aggregate_bytes = 0
    snapshot_bytes = 0
    for index, operation in enumerate(operations):
        row = {"index": index, "status": "rejected"}
        try:
            if not isinstance(operation, dict):
                raise ValueError("operation must be an object")
            unknown = sorted(set(operation) - {"path", "content", "mode"})
            if unknown:
                raise ValueError(
                    "unsupported operation field(s): %s" % ", ".join(unknown)
                )
            path = operation.get("path")
            content = operation.get("content")
            mode = operation.get("mode")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("path must be a non-empty string")
            if _foreign_absolute(path.strip()):
                raise PermissionError("path uses a non-native absolute form")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            if mode not in {"create", "overwrite"}:
                raise ValueError("mode must explicitly be create or overwrite")
            encoded = content.encode("utf-8")
            if len(encoded) > MAX_WRITE_BYTES:
                raise ValueError("content exceeds max write bytes")
            aggregate_bytes += len(encoded)
            if aggregate_bytes > MAX_BATCH_BYTES:
                raise ValueError(
                    "aggregate content exceeds max batch bytes (%d)"
                    % MAX_BATCH_BYTES
                )
            requested = _batch_requested_path(path)
            _require_no_reparse_components(requested)
            resolved = resolve_path(
                path, extra_roots=extra_roots, bypass=bypass,
            )
            authorized_roots = allowed_roots(extra_roots if bypass else "")
            authorized_root = next((
                root for root in authorized_roots
                if resolved == root or _is_inside(resolved, root)
            ), None)
            if authorized_root is None:
                raise PermissionError("path is outside every authorized root")
            # Batch writes are intended for ordinary project files.  Unlike the
            # developer-token escape hatch on the single-file tools, a batch
            # can never include runtime control state or credential material.
            relative = resolved.relative_to(authorized_root)
            if (
                _is_protected_mutation_path(resolved)
                or any(
                    part.lower() in SENSITIVE_READ_DIRECTORIES
                    for part in relative.parts
                )
            ):
                raise PermissionError("batch target is secret or control state")
            key = os.path.normcase(os.path.normpath(str(resolved)))
            if key in seen:
                raise ValueError("duplicate batch target")
            seen.add(key)
            exists = resolved.exists()
            metadata = None
            if exists:
                metadata = resolved.stat()
                if not resolved.is_file() or _is_reparse_point(resolved):
                    raise ValueError("batch target exists but is not a regular file")
                identity = (metadata.st_dev, metadata.st_ino)
                if metadata.st_ino and identity in seen_existing_identities:
                    raise ValueError("duplicate batch target by file identity")
                if metadata.st_ino:
                    seen_existing_identities.add(identity)
            if mode == "create" and exists:
                raise FileExistsError("file exists; use mode=overwrite")
            if mode == "overwrite" and not exists:
                raise FileNotFoundError("overwrite target does not exist")
            original = b""
            if exists:
                size = metadata.st_size
                if size > MAX_WRITE_BYTES:
                    raise ValueError("existing file exceeds rollback snapshot bytes")
                snapshot_bytes += size
                if snapshot_bytes > MAX_BATCH_SNAPSHOT_BYTES:
                    raise ValueError(
                        "existing files exceed aggregate rollback snapshot bytes (%d)"
                        % MAX_BATCH_SNAPSHOT_BYTES
                    )
                original = resolved.read_bytes()
            missing_parents = []
            cursor = resolved.parent
            while not cursor.exists():
                missing_parents.append(cursor)
                if cursor.parent == cursor:
                    break
                cursor = cursor.parent
            row.update({
                "path": str(resolved), "mode": mode, "status": "ready",
                "bytes": len(encoded),
            })
            prepared.append({
                "index": index,
                "path": resolved,
                "requested_path": path,
                "content": content,
                "mode": mode,
                "existed": exists,
                "original": original,
                "identity": (
                    (metadata.st_dev, metadata.st_ino)
                    if metadata is not None and metadata.st_ino else None
                ),
                "missing_parents": missing_parents,
            })
        except (OSError, TypeError, ValueError) as exc:
            row["error"] = str(exc)
        results.append(row)
    rejected = [row for row in results if row["status"] == "rejected"]
    if rejected:
        report = {
            "ok": False,
            "transaction": "not_started",
            "count": len(operations),
            "aggregate_bytes": aggregate_bytes,
            "snapshot_bytes": snapshot_bytes,
            "results": results,
        }
        raise BatchWriteError(
            "batch prevalidation rejected %d operation(s)" % len(rejected), report,
        )
    return prepared, aggregate_bytes, snapshot_bytes


def _rollback_batch(prepared: list[dict]) -> list[dict]:
    rollback = []
    for item in reversed(prepared):
        path = item["path"]
        row = {"index": item["index"], "path": str(path), "restored": False}
        try:
            _require_no_reparse_components(path)
            if _resolve_best_effort(path) != path:
                raise OSError("rollback target resolution changed")
            if item["existed"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item["original"])
                row.update({"restored": True, "action": "restore"})
            elif path.exists():
                if not path.is_file() or _is_reparse_point(path):
                    raise OSError("created target is no longer a regular file")
                path.unlink()
                row.update({"restored": True, "action": "remove_created"})
            else:
                row.update({"restored": True, "action": "already_absent"})
        except OSError as exc:
            row["error"] = str(exc)
        rollback.append(row)
    directories = {
        directory
        for item in prepared
        for directory in item["missing_parents"]
    }
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    rollback.sort(key=lambda row: row["index"])
    return rollback


def batch_write_files(
    operations,
    *,
    extra_roots: str = "",
    bypass: bool = False,
) -> dict:
    """Apply a bounded create/overwrite list with best-effort rollback.

    Every target, payload, mode, and rollback snapshot is validated before the
    first call to ``write_file``.  This function never permits append semantics,
    protected control/secret targets, duplicate targets, or reparse components.
    """
    payload = _batch_payload(operations)
    with _BATCH_WRITE_LOCK:
        prepared, aggregate_bytes, snapshot_bytes = _batch_preflight(
            payload, extra_roots=extra_roots, bypass=bypass,
        )
        results = []
        attempted_count = 0
        try:
            for item in prepared:
                _require_no_reparse_components(
                    _batch_requested_path(item["requested_path"])
                )
                if resolve_path(
                    item["requested_path"],
                    extra_roots=extra_roots,
                    bypass=bypass,
                ) != item["path"]:
                    raise PermissionError("batch target resolution changed after preflight")
                if item["identity"] is not None:
                    current = item["path"].stat()
                    if (current.st_dev, current.st_ino) != item["identity"]:
                        raise PermissionError("batch target identity changed after preflight")
                result = write_file(
                    item["requested_path"],
                    item["content"],
                    mode=item["mode"],
                    extra_roots=extra_roots,
                    bypass=bypass,
                    developer_authorized=False,
                )
                results.append({
                    "index": item["index"],
                    "path": result["path"],
                    "mode": item["mode"],
                    "status": "written",
                    "action": result["action"],
                    "bytes": result["bytes"],
                    "lines_added": result.get("lines_added", 0),
                    "lines_edited": result.get("lines_edited", 0),
                    "lines_deleted": result.get("lines_deleted", 0),
                    "created_directories": result.get("created_directories", []),
                })
                attempted_count += 1
        except Exception as exc:
            attempted = prepared[:attempted_count]
            rollback = _rollback_batch(attempted)
            complete = all(row["restored"] for row in rollback)
            rolled_back = {row["index"]: row for row in rollback}
            transaction_results = []
            written_by_index = {row["index"]: row for row in results}
            failed_index = prepared[attempted_count]["index"]
            for item in prepared:
                index = item["index"]
                if index in written_by_index:
                    row = dict(written_by_index[index])
                    row["status"] = (
                        "rolled_back"
                        if rolled_back.get(index, {}).get("restored")
                        else "rollback_failed"
                    )
                elif index == failed_index:
                    row = {
                        "index": index, "path": str(item["path"]),
                        "mode": item["mode"], "status": "failed",
                        "error": str(exc),
                    }
                else:
                    row = {
                        "index": index, "path": str(item["path"]),
                        "mode": item["mode"], "status": "not_attempted",
                    }
                transaction_results.append(row)
            report = {
                "ok": False,
                "transaction": "rolled_back" if complete else "rollback_incomplete",
                "count": len(prepared),
                "aggregate_bytes": aggregate_bytes,
                "snapshot_bytes": snapshot_bytes,
                "failed_index": failed_index,
                "error": str(exc),
                "results": transaction_results,
                "rollback": rollback,
            }
            raise BatchWriteError("batch write failed: %s" % exc, report) from exc
    return {
        "ok": True,
        "transaction": "committed",
        "count": len(results),
        "aggregate_bytes": aggregate_bytes,
        "snapshot_bytes": snapshot_bytes,
        "results": results,
    }


def edit_file(
    path: str,
    old: str,
    new: str,
    *,
    count: int = 1,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    p = resolve_mutation_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    if old == "":
        raise ValueError("old text must not be empty")
    # The mutation guard above already cleared this path, so the same
    # authorization satisfies the read guard for the read-modify-write cycle.
    current = read_file(
        path, extra_roots=extra_roots, bypass=bypass,
        developer_authorized=developer_authorized,
    )
    if current["truncated"]:
        raise ValueError("file too large for safe text edit")
    text = current["text"]
    max_count = max(1, min(1000, int(count or 1)))
    occurrences = text.count(old)
    if occurrences == 0:
        raise ValueError("old text not found")
    next_text = text.replace(old, new or "", max_count)
    result = write_file(
        path,
        next_text,
        mode="overwrite",
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    replacements = min(occurrences, max_count)
    result["replacements"] = min(occurrences, max_count)
    result["action"] = "edit"
    result["lines_added"] = _line_count(new or "") * replacements
    result["lines_deleted"] = _line_count(old or "") * replacements
    result["lines_edited"] = replacements
    return result


def delete_path(
    path: str,
    *,
    recursive: bool = False,
    dry_run: bool = True,
    confirm: str = "",
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    requested = _requested_path(path)
    p = resolve_mutation_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    if recursive:
        _require_safe_recursive_delete(
            requested,
            p,
            extra_roots=extra_roots,
            bypass=bypass,
            developer_authorized=developer_authorized,
        )
    exists = p.exists()
    line_count = _line_count_on_disk(p)
    required = "DELETE %s" % p
    if dry_run or confirm != required:
        return {
            "path": str(p),
            "exists": exists,
            "dry_run": True,
            "deleted": False,
            "lines_deleted": 0,
            "would_delete_lines": line_count,
            "required_confirm": required,
        }
    if not exists:
        return {
            "path": str(p),
            "exists": False,
            "dry_run": False,
            "deleted": False,
            "lines_deleted": 0,
        }
    if p.is_dir():
        if not recursive:
            raise ValueError("directory delete requires recursive=True")
        if developer_authorized:
            for child in sorted(p.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
        else:
            _delete_tree_guarded(p)
        p.rmdir()
    else:
        p.unlink()
    return {
        "path": str(p),
        "exists": True,
        "dry_run": False,
        "deleted": True,
        "action": "delete",
        "lines_deleted": line_count,
    }


# --- structured data inspection ---------------------------------------------

INSPECT_PREVIEW_ITEMS = 20
INSPECT_PREVIEW_CHARS = 2000


class _InspectSizeError(Exception):
    """Raised when a file exceeds the inspection byte budget."""


def _inspect_text_payload(p: Path, max_bytes: int) -> str:
    size = p.stat().st_size
    if size > max_bytes:
        raise _InspectSizeError(size)
    return p.read_text(encoding="utf-8", errors="replace")


def _inspect_json(p: Path, max_bytes: int) -> dict:
    import json

    payload = json.loads(_inspect_text_payload(p, max_bytes))
    out = {"kind": "json", "type": type(payload).__name__}
    if isinstance(payload, dict):
        keys = list(payload.keys())
        out["keys"] = ", ".join(str(k) for k in keys[:INSPECT_PREVIEW_ITEMS])
        out["key_count"] = len(keys)
    elif isinstance(payload, list):
        out["items"] = len(payload)
        if payload and isinstance(payload[0], dict):
            out["item_keys"] = ", ".join(
                str(k) for k in list(payload[0].keys())[:INSPECT_PREVIEW_ITEMS]
            )
    preview = json.dumps(payload, ensure_ascii=False, indent=2)
    out["text"] = preview[:INSPECT_PREVIEW_CHARS] + (
        "\n… (truncated)" if len(preview) > INSPECT_PREVIEW_CHARS else ""
    )
    return out


def _inspect_jsonl(p: Path, max_bytes: int) -> dict:
    import json

    text = _inspect_text_payload(p, max_bytes)
    lines = [line for line in text.splitlines() if line.strip()]
    out = {"kind": "jsonl", "records": len(lines)}
    if lines:
        try:
            first = json.loads(lines[0])
            if isinstance(first, dict):
                out["record_keys"] = ", ".join(
                    str(k) for k in list(first.keys())[:INSPECT_PREVIEW_ITEMS]
                )
        except ValueError:
            out["note"] = "first line is not valid JSON"
    return out


def _inspect_toml(p: Path, max_bytes: int) -> dict:
    import tomllib

    with p.open("rb") as fh:
        payload = tomllib.load(fh)
    tables = list(payload.keys())
    return {
        "kind": "toml",
        "tables": ", ".join(tables[:INSPECT_PREVIEW_ITEMS]),
        "table_count": len(tables),
    }


def _inspect_yaml(p: Path, max_bytes: int) -> dict:
    try:
        import yaml  # optional dependency
    except ImportError:
        return {
            "kind": "yaml",
            "note": "PyYAML is not installed; showing raw head only",
            "text": _inspect_text_payload(p, max_bytes)[:INSPECT_PREVIEW_CHARS],
        }
    payload = yaml.safe_load(_inspect_text_payload(p, max_bytes))
    out = {"kind": "yaml", "type": type(payload).__name__}
    if isinstance(payload, dict):
        out["keys"] = ", ".join(
            str(k) for k in list(payload.keys())[:INSPECT_PREVIEW_ITEMS]
        )
    elif isinstance(payload, list):
        out["items"] = len(payload)
    return out


def _inspect_csv(p: Path, max_bytes: int, delimiter: str) -> dict:
    import csv
    import io

    text = _inspect_text_payload(p, max_bytes)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    out = {
        "kind": "tsv" if delimiter == "\t" else "csv",
        "rows": max(0, len(rows) - 1),
    }
    if rows:
        out["columns"] = ", ".join(rows[0][:INSPECT_PREVIEW_ITEMS])
        out["column_count"] = len(rows[0])
        if len(rows) > 1:
            sample = rows[1][:INSPECT_PREVIEW_ITEMS]
            out["sample_row"] = ", ".join(
                cell[:40] for cell in sample
            )
    return out


def _inspect_sqlite(p: Path) -> dict:
    import sqlite3

    uri = "file:%s?mode=ro" % p.as_posix()
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for table in tables[:INSPECT_PREVIEW_ITEMS]:
            try:
                counts[table] = conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % table.replace('"', '""')
                ).fetchone()[0]
            except sqlite3.Error:
                counts[table] = "?"
    finally:
        conn.close()
    return {
        "kind": "sqlite",
        "tables": len(tables),
        "text": "\n".join(
            "%s: %s rows" % (name, counts[name]) for name in counts
        ) or "(no tables)",
    }


def _inspect_zip(p: Path) -> dict:
    import zipfile

    with zipfile.ZipFile(p) as archive:
        names = archive.namelist()
        total = sum(info.file_size for info in archive.infolist())
    listing = "\n".join(names[:INSPECT_PREVIEW_ITEMS])
    if len(names) > INSPECT_PREVIEW_ITEMS:
        listing += "\n… (%d more)" % (len(names) - INSPECT_PREVIEW_ITEMS)
    return {
        "kind": "zip",
        "members": len(names),
        "expanded_bytes": total,
        "text": listing,
    }


def _inspect_tar(p: Path) -> dict:
    import tarfile

    with tarfile.open(p) as archive:
        members = archive.getmembers()
    names = [m.name for m in members]
    listing = "\n".join(names[:INSPECT_PREVIEW_ITEMS])
    if len(names) > INSPECT_PREVIEW_ITEMS:
        listing += "\n… (%d more)" % (len(names) - INSPECT_PREVIEW_ITEMS)
    return {
        "kind": "tar",
        "members": len(names),
        "expanded_bytes": sum(m.size for m in members),
        "text": listing,
    }


def _inspect_ini(p: Path, max_bytes: int) -> dict:
    import configparser

    parser = configparser.ConfigParser()
    parser.read_string(_inspect_text_payload(p, max_bytes))
    sections = parser.sections()
    return {
        "kind": "ini",
        "sections": ", ".join(sections[:INSPECT_PREVIEW_ITEMS]),
        "section_count": len(sections),
    }


_INSPECTORS = {
    ".json": lambda p, m: _inspect_json(p, m),
    ".jsonl": lambda p, m: _inspect_jsonl(p, m),
    ".ndjson": lambda p, m: _inspect_jsonl(p, m),
    ".toml": lambda p, m: _inspect_toml(p, m),
    ".yaml": lambda p, m: _inspect_yaml(p, m),
    ".yml": lambda p, m: _inspect_yaml(p, m),
    ".csv": lambda p, m: _inspect_csv(p, m, ","),
    ".tsv": lambda p, m: _inspect_csv(p, m, "\t"),
    ".db": lambda p, m: _inspect_sqlite(p),
    ".sqlite": lambda p, m: _inspect_sqlite(p),
    ".sqlite3": lambda p, m: _inspect_sqlite(p),
    ".zip": lambda p, m: _inspect_zip(p),
    ".tar": lambda p, m: _inspect_tar(p),
    ".tgz": lambda p, m: _inspect_tar(p),
    ".ini": lambda p, m: _inspect_ini(p, m),
    ".cfg": lambda p, m: _inspect_ini(p, m),
}


def inspect_data(
    path: str,
    *,
    max_bytes: int = MAX_READ_BYTES,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    """Structured, read-only preview of a data file inside allowed roots.

    Understands JSON/JSONL/TOML/YAML/CSV/TSV/SQLite/ZIP/TAR/INI by suffix
    and falls back to text statistics or a binary signature preview. Never
    executes content and never returns more than a bounded preview.
    """
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_read_access(p, developer_authorized or bypass)
    if not p.exists():
        raise ValueError("no such file: %s" % p)
    if p.is_dir():
        raise ValueError("%s is a directory; use directory_tree" % p)
    size = p.stat().st_size
    suffix = p.suffix.lower()
    if suffix == ".gz" and p.name.lower().endswith(".tar.gz"):
        suffix = ".tgz"
    base = {"path": str(p), "bytes": size}
    inspector = _INSPECTORS.get(suffix)
    if inspector is not None:
        try:
            result = inspector(p, max_bytes)
        except _InspectSizeError:
            # Size-guard failures are caller errors: surface them, do not
            # swallow into a soft result.
            raise ValueError(
                "file is %d bytes; inspection parses at most %d"
                % (size, max_bytes)
            )
        except Exception as exc:
            # Malformed content (bad JSON/TOML/CSV, corrupt archive) is a
            # reportable finding, not a crash.
            result = {
                "kind": suffix.lstrip("."),
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            }
        base.update(result)
        return base
    # Unknown suffix: text stats when it decodes, binary signature otherwise.
    head = p.open("rb").read(min(size, 4096))
    if b"\x00" in head:
        base.update({
            "kind": "binary",
            "signature": head[:16].hex(" "),
        })
        return base
    if size > max_bytes:
        base.update({"kind": "text", "note": "too large to scan fully"})
        return base
    text = p.read_text(encoding="utf-8", errors="replace")
    base.update({
        "kind": "text",
        "lines": _line_count(text),
        "text": text[:INSPECT_PREVIEW_CHARS] + (
            "\n… (truncated)" if len(text) > INSPECT_PREVIEW_CHARS else ""
        ),
    })
    return base
