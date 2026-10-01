"""Host-owned workspaces and receipts for greenfield build fleets.

The model is never trusted to choose a path or to report what it produced.  A
creation workspace is provisioned by the host, one directory per worker, and
the host scans that directory after the guarded worker returns.
"""
from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from sonder_runtime.platform.paths import default_home


# Host-created roots are the authority for receipts.  Keeping this registry
# process-local means a model cannot turn a receipt call into an arbitrary
# directory scan by supplying a path that merely looks like a worker folder.
_PROVISIONED_WORKERS: set[str] = set()


@dataclass(frozen=True)
class CreationWorkspace:
    root: Path
    workers: tuple[Path, ...]


def is_provisioned_worker(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* is an exact host-provisioned worker root.

    This is intentionally an exact lookup rather than an ``is_relative_to``
    check: a delegated model must never be able to turn a parent or sibling
    directory into its project scope.
    """
    try:
        candidate = Path(os.path.abspath(path))
        if not candidate.is_dir() or _reparse(candidate):
            return False
        canonical = os.path.normcase(str(Path(os.path.realpath(candidate))))
    except (OSError, TypeError, ValueError):
        return False
    return canonical in _PROVISIONED_WORKERS


def _reparse(path: Path) -> bool:
    """Return whether *path* is a symlink or Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        attrs = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(path.stat(), "st_file_attributes", 0) & attrs)
    except OSError as exc:
        raise PermissionError("creation workspace path cannot be inspected") from exc


def _contained(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.commonpath((str(path), str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _checked_root(path: Path, root: Path) -> Path:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if not _contained(path, root):
        raise PermissionError("creation workspace path escapes its host root")
    if root.exists() and _reparse(root):
        raise PermissionError("creation workspace rejects symlink or reparse root")
    current = root
    relative = path.relative_to(root)
    for part in relative.parts:
        current = current / part
        if current.exists() and _reparse(current):
            raise PermissionError("creation workspace rejects symlink or reparse path")
    resolved = Path(os.path.realpath(path))
    if not _contained(resolved, root):
        raise PermissionError("creation workspace path resolves outside its host root")
    return path


def create_workspace(master_id: str, worker_count: int, *, state_home: str | os.PathLike[str] | None = None) -> CreationWorkspace:
    """Create a fresh, strictly contained worker workspace for one master."""
    if not isinstance(master_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", master_id):
        raise ValueError("invalid master id")
    if int(worker_count) < 1:
        raise ValueError("worker_count must be positive")
    home = Path(state_home).expanduser() if state_home is not None else Path(default_home())
    home = Path(os.path.abspath(home))
    if home.exists() and _reparse(home):
        raise PermissionError("state home cannot be a symlink or reparse point")
    root = _checked_root(home / "creations" / master_id, home)
    root.mkdir(parents=True, exist_ok=False)
    workers = []
    provisioned = []
    try:
        for index in range(1, int(worker_count) + 1):
            worker = _checked_root(root / ("worker-%02d" % index), root)
            worker.mkdir()
            workers.append(worker)
            canonical = os.path.normcase(str(Path(os.path.realpath(worker))))
            _PROVISIONED_WORKERS.add(canonical)
            provisioned.append(canonical)
    except BaseException:
        for canonical in provisioned:
            _PROVISIONED_WORKERS.discard(canonical)
        # No worker has been dispatched yet; remove only our empty directories.
        for worker in reversed(workers):
            with contextlib.suppress(OSError):
                worker.rmdir()
        with contextlib.suppress(OSError):
            root.rmdir()
        raise
    return CreationWorkspace(root=root, workers=tuple(workers))


def _files(root: Path) -> tuple[str, ...]:
    result = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = _checked_root(Path(current), root)
        dirs[:] = sorted(dirs)
        files = sorted(files)
        for name in tuple(dirs) + tuple(files):
            path = _checked_root(current_path / name, root)
            if _reparse(path):
                raise PermissionError("worker output contains symlink or reparse path")
        for name in files:
            result.append(str((current_path / name).relative_to(root)))
    return tuple(sorted(result))


def inventory_files(worker_root: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return host-observed files for an issued worker root."""
    root = Path(os.path.abspath(worker_root))
    if not is_provisioned_worker(root):
        raise PermissionError("worker receipt path was not provisioned by the host")
    return _files(_checked_root(root, root))


def attach_receipt(result, worker_root: str | os.PathLike[str]):
    """Attach host-observed files and check status to a worker result."""
    from dataclasses import replace

    root = Path(os.path.abspath(worker_root))
    canonical = os.path.normcase(str(Path(os.path.realpath(root))))
    if canonical not in _PROVISIONED_WORKERS:
        raise PermissionError("worker receipt path was not provisioned by the host")
    root = _checked_root(root, root)
    produced = inventory_files(root)
    attempted = getattr(result, "checks_run", None)
    if attempted is None:
        attempted = getattr(result, "validation_attempted", False)
    passed = getattr(result, "checks_passed", None)
    if passed is None:
        passed = getattr(result, "validation_passed", None)
    if hasattr(result, "produced_files"):
        return replace(result, produced_files=produced, checks_run=bool(attempted), checks_passed=passed)
    return result


def host_report(worker_id: str, worker_root: str | os.PathLike[str], result) -> str:
    """Return a deterministic host receipt suitable for aggregate output."""
    files = tuple(getattr(result, "produced_files", ()) or ())
    attempted = bool(getattr(result, "checks_run", False))
    passed = getattr(result, "checks_passed", None)
    check = "not run" if not attempted else ("passed" if passed else "failed")
    lines = ["worker=%s", "folder=%s", "files=%d", "checks=%s"]
    text = "\n".join(lines) % (worker_id, str(Path(worker_root)), len(files), check)
    if files:
        text += "\nproduced:\n" + "\n".join("- " + value for value in files)
    return text


def release_workspace(workspace: CreationWorkspace) -> None:
    """Release process-local receipt authority after aggregation completes."""
    for worker in workspace.workers:
        _PROVISIONED_WORKERS.discard(
            os.path.normcase(str(Path(os.path.realpath(worker))))
        )
