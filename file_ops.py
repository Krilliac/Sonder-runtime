"""Guarded filesystem operations for Sonder tools.

Default policy is intentionally conservative: read/write/delete are limited to
approved roots, file sizes are bounded, and deletes dry-run unless explicitly
confirmed. Broader system access requires an explicit bypass decision by the
server layer.
"""
from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

import sonder_paths


MAX_READ_BYTES = 256_000
MAX_WRITE_BYTES = 1_000_000
MAX_FIND_RESULTS = 200
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


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines()) or 1


def _read_text_if_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def workspace_root() -> Path:
    return Path(__file__).resolve().parent


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
        else Path(sonder_paths.default_home()) / DEFAULT_ROOTS_FILE
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


def allowed_roots(extra_roots: str = "") -> list[Path]:
    roots = [workspace_root(), Path(sonder_paths.default_home())]
    roots.extend(_split_roots(os.environ.get("SONDER_FILE_ROOTS", "")))
    roots.extend(_roots_from_file())
    roots.extend(_split_roots(extra_roots))
    out = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root.absolute()
        if resolved not in out:
            out.append(resolved)
    return out


def bypass_enabled() -> bool:
    return os.environ.get("SONDER_FILE_BYPASS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _resolve_best_effort(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _workspace_config_path(env_name: str, default_name: str) -> Path:
    raw = os.environ.get(env_name, "").strip()
    path = Path(raw).expanduser() if raw else workspace_root() / default_name
    if not path.is_absolute():
        path = workspace_root() / path
    return _resolve_best_effort(path)


def _control_plane_paths() -> set[Path]:
    root = _resolve_best_effort(workspace_root())
    home = _resolve_best_effort(Path(sonder_paths.default_home()))
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
        db, Path(str(db) + "-wal"), Path(str(db) + "-shm"),
        root / "memory.db", root / "memory.db-wal", root / "memory.db-shm",
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
        or name in {"memory.db", "memory.db-wal", "memory.db-shm"}
        or _is_secret_path(path)
    )


def _is_protected_mutation_path(path: Path) -> bool:
    path = _resolve_best_effort(path)
    if _is_sensitive_control_path(path):
        return True
    root = _resolve_best_effort(workspace_root())
    return path.suffix.lower() == ".py" and path.parent == root


def _require_mutation_access(path: Path, developer_authorized: bool) -> None:
    if _is_protected_mutation_path(path) and not developer_authorized:
        raise PermissionError(
            "refusing to mutate protected Sonder control-plane path "
            "without an authenticated developer token: %s" % path
        )


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
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
                "refusing recursive delete through a symlink or junction without "
                "an authenticated developer token: %s" % current
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
                        "or junction without an authenticated developer token: %s" % child
                    )
                if _is_sensitive_control_path(child) or _is_protected_mutation_path(child):
                    raise PermissionError(
                        "refusing recursive deletion of a tree containing protected "
                        "control state without an authenticated developer token: %s" % child
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
        resolved = candidate.absolute()
    roots = allowed_roots(extra_roots if bypass else "")
    if bypass:
        return resolved
    if any(_is_inside(resolved, root) for root in roots):
        return resolved
    raise PermissionError(
        "path is outside allowed roots. Set SONDER_FILE_ROOTS or use an approved admin/dev bypass."
    )


def policy_text(*, bypass: bool = False, extra_roots: str = "") -> str:
    lines = [
        "filesystem policy",
        "  mode: %s" % ("bypass" if bypass else "guarded"),
        "  delete default: dry-run unless confirm matches DELETE <path>",
        "  max read bytes: %d" % MAX_READ_BYTES,
        "  max write bytes: %d" % MAX_WRITE_BYTES,
        "  roots:",
    ]
    for root in allowed_roots(extra_roots if bypass else ""):
        lines.append("    - %s" % root)
    lines.extend([
        "  hot roots file: %s" % roots_file_path(),
        "  env bypass: SONDER_FILE_BYPASS=1",
        "  approval code: SONDER_FILE_APPROVAL_CODE plus approval=<code>",
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
) -> dict:
    root_path = resolve_path(root or ".", extra_roots=extra_roots, bypass=bypass)
    if not root_path.exists():
        raise FileNotFoundError("search root not found: %s" % root_path)
    if not root_path.is_dir():
        raise ValueError("root is not a directory: %s" % root_path)
    pattern = (query or "*").strip() or "*"
    limit = max(1, min(MAX_FIND_RESULTS, int(max_results or 50)))
    results = []
    for base, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".pytest_cache", "venv", "__pycache__"}]
        names = dirs + files
        for name in names:
            rel = str((Path(base) / name).relative_to(root_path))
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern) or pattern.lower() in rel.lower():
                p = Path(base) / name
                results.append({
                    "path": str(p),
                    "relative": rel,
                    "type": "dir" if p.is_dir() else "file",
                    "bytes": p.stat().st_size if p.is_file() else 0,
                })
                if len(results) >= limit:
                    # Hit the cap with the walk unfinished: more may match.
                    # Signal it so counting callers don't silently undercount.
                    return {"root": str(root_path), "query": pattern,
                            "results": results, "truncated": True, "limit": limit}
    return {"root": str(root_path), "query": pattern, "results": results,
            "truncated": False, "limit": limit}


def read_file(path: str, *, max_bytes: int = MAX_READ_BYTES, extra_roots: str = "", bypass: bool = False) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
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
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
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


def write_file(
    path: str,
    content: str,
    *,
    mode: str = "create",
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict:
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    before = _read_text_if_file(p)
    before_lines = _line_count(before)
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
    after_lines = _line_count(before + (content or "")) if mode == "append" else _line_count(content or "")
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
    return {
        "path": str(p),
        "bytes": p.stat().st_size,
        "mode": mode,
        "action": action,
        "lines_before": before_lines,
        "lines_after": after_lines,
        "lines_added": lines_added,
        "lines_edited": lines_edited,
        "lines_deleted": lines_deleted,
        "created_directories": list(reversed(missing_parents)),
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
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
    _require_mutation_access(p, developer_authorized)
    if old == "":
        raise ValueError("old text must not be empty")
    current = read_file(path, extra_roots=extra_roots, bypass=bypass)
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
    p = resolve_path(path, extra_roots=extra_roots, bypass=bypass)
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
    line_count = _line_count(_read_text_if_file(p))
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
