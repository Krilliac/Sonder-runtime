"""Pure contracts and scope rules for resume workspace reality checks."""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, TypedDict


class DirtyEntry(TypedDict):
    status: str
    path: str
    size: int
    mtime_ns: int


class WorkspaceSnapshot(TypedDict, total=False):
    version: int
    root: str
    head: str | None
    branch: str | None
    dirty_hash: str
    dirty: list[DirtyEntry]
    complete: bool
    reason: str


class WorkspaceDelta(TypedDict, total=False):
    status: str
    previous_head: str | None
    current_head: str | None
    history_rewritten: bool | None
    ahead: int | None
    behind: int | None
    commits: list[dict[str, str]]
    files: list[dict[str, str]]
    truncated: bool
    requires_reinspection: bool
    requires_replan: bool
    reason: str
    snapshot: WorkspaceSnapshot


class WorkspaceRealityPort(Protocol):
    def capture(self, root: str, *, deadline_monotonic: float | None = None) -> WorkspaceSnapshot | None: ...

    def revalidate(
        self, root: str, snapshot: Mapping[str, Any], owned_paths: Sequence[str] = (),
        *, deadline_monotonic: float | None = None,
    ) -> WorkspaceDelta | None: ...


def scope_intersects(
    paths: Sequence[str] | None,
    files: Sequence[Mapping[str, str]],
    root: str,
) -> bool:
    """Return whether a declared scope intersects changed repository files.

    An omitted or empty scope is unknown and therefore conservative.  Entries
    accept repository-relative paths, absolute paths, directories, and glob
    patterns.  A changed file with an unknown/empty path also conservatively
    intersects a declared scope.
    """
    if not paths:
        return True
    from fnmatch import fnmatchcase
    from pathlib import Path, PurePosixPath

    root_path = Path(root).resolve()
    normalized_scope: list[tuple[str, bool]] = []
    for raw in paths:
        value = str(raw).replace("\\", "/")
        if not value:
            return True
        absolute = Path(value).is_absolute()
        if absolute:
            try:
                value = Path(value).resolve().relative_to(root_path).as_posix()
            except ValueError:
                continue
        while value.startswith("./"):
            value = value[2:]
        if value in {"", "."}:
            return True
        if value == ".." or value.startswith("../") or "/../" in value:
            return True
        has_glob = any(mark in value for mark in "*?[")
        normalized_scope.append((value.rstrip("/").casefold(), has_glob))
    if not normalized_scope:
        return True
    for item in files:
        changed = str(item.get("path", "")).replace("\\", "/")
        while changed.startswith("./"):
            changed = changed[2:]
        if not changed:
            return True
        if changed == ".." or changed.startswith("../") or "/../" in changed:
            return True
        changed = changed.casefold()
        changed_path = PurePosixPath(changed)
        for scope, has_glob in normalized_scope:
            if has_glob and (fnmatchcase(changed, scope) or changed_path.match(scope)):
                return True
            if not has_glob and (changed == scope or changed.startswith(scope + "/")):
                return True
    return False


__all__ = [
    "DirtyEntry", "WorkspaceSnapshot", "WorkspaceDelta", "WorkspaceRealityPort",
    "scope_intersects",
]
