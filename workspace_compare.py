"""Bounded metadata-only comparison of two guarded files or directories."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

import file_ops


DEFAULT_MAX_ENTRIES = 2_000
MAX_ENTRIES = 10_000
DEFAULT_MAX_FILE_BYTES = 64_000_000
MAX_FILE_BYTES = 256_000_000
DEFAULT_MAX_TOTAL_BYTES = 256_000_000
MAX_TOTAL_BYTES = 1_000_000_000
DEFAULT_MAX_DETAILS = 1_000
MAX_DETAILS = 10_000
DEFAULT_OUTPUT_BYTES = 256_000
MAX_OUTPUT_BYTES = 512_000
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 15.0
HASH_CHUNK_BYTES = 1 << 20


class WorkspaceCompareError(RuntimeError):
    """A rejected path, unsafe entry, race, or exhausted comparison budget."""


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_timeout(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(0.05, min(value, MAX_TIMEOUT_SECONDS))


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino)


def _check_deadline(budget):
    if time.monotonic() >= budget["deadline"]:
        raise WorkspaceCompareError("workspace comparison exceeded the timeout ceiling")


def _requested_path(raw):
    text = str(raw or "").strip()
    if not text:
        raise WorkspaceCompareError("comparison path must be non-empty")
    if "\x00" in text or file_ops._foreign_absolute(text):
        raise WorkspaceCompareError("comparison path uses an unsafe absolute form")
    return file_ops._requested_path(text)


def _reject_reparse_components(requested):
    current = Path(os.path.normpath(str(requested)))
    while True:
        if file_ops._is_reparse_point(current):
            raise WorkspaceCompareError(
                "comparison path must not traverse a symlink or junction"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _authorized_root(target, *, extra_roots, bypass):
    roots = [
        file_ops._resolve_best_effort(root)
        for root in file_ops.allowed_roots(extra_roots if bypass else "")
    ]
    return next((
        root for root in roots
        if target == root or file_ops._is_inside(target, root)
    ), None)


def _sensitive(target, root):
    relative = target.relative_to(root) if target != root else Path(".")
    return (
        file_ops._is_protected_read_path(target)
        or any(
            part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
            for part in root.parts
        )
        or any(
            part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
            for part in relative.parts
        )
    )


def resolve_compare_root(
    raw, *, extra_roots="", bypass=False, developer_authorized=False,
):
    requested = _requested_path(raw)
    _reject_reparse_components(requested)
    try:
        target = file_ops.require_read_access(
            str(raw), extra_roots=extra_roots, bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise WorkspaceCompareError("comparison path rejected: %s" % exc) from exc
    authorized = _authorized_root(
        target, extra_roots=extra_roots, bypass=bypass,
    )
    if authorized is None:
        raise WorkspaceCompareError("comparison path is outside authorized roots")
    if _sensitive(target, authorized):
        raise WorkspaceCompareError("comparison path is secret or control state")
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise WorkspaceCompareError("comparison path cannot be inspected") from exc
    if file_ops._is_reparse_point(target):
        raise WorkspaceCompareError("comparison path must not be a symlink or junction")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise WorkspaceCompareError("comparison path must be a regular file or directory")
    return target, authorized


def _reserve_entry(budget):
    _check_deadline(budget)
    budget["entries"] += 1
    if budget["entries"] > budget["max_entries"]:
        raise WorkspaceCompareError("workspace comparison exceeded the entry ceiling")


def _hash_file(path, budget):
    _check_deadline(budget)
    try:
        before = path.lstat()
    except OSError as exc:
        raise WorkspaceCompareError("file metadata changed during comparison") from exc
    if not stat.S_ISREG(before.st_mode) or file_ops._is_reparse_point(path):
        raise WorkspaceCompareError("comparison encountered a non-regular file")
    if not before.st_ino:
        raise WorkspaceCompareError("file identity is unavailable for safe hashing")
    if before.st_size > budget["max_file_bytes"]:
        raise WorkspaceCompareError("comparison file exceeds the per-file byte ceiling")
    if budget["bytes"] + before.st_size > budget["max_total_bytes"]:
        raise WorkspaceCompareError("workspace comparison exceeded the total byte ceiling")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceCompareError("file could not be safely opened for hashing") from exc
    digest = hashlib.sha256()
    read_bytes = 0
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or _identity(opened_before) != _identity(before)
        ):
            raise WorkspaceCompareError("file identity changed before hashing")
        while True:
            _check_deadline(budget)
            chunk = os.read(descriptor, HASH_CHUNK_BYTES)
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > before.st_size:
                raise WorkspaceCompareError("file grew while it was being hashed")
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise WorkspaceCompareError("file path changed after hashing") from exc
    # ctime is not a content-stability signal and Windows can expose different
    # ctime values through a path stat and an opened-handle stat.  Identity,
    # size, mtime, and the no-follow handle are the portable race checks here.
    stable_fields = ("st_size", "st_mtime_ns")
    if (
        read_bytes != before.st_size
        or _identity(opened_after) != _identity(before)
        or _identity(after) != _identity(before)
        or any(getattr(opened_after, name) != getattr(before, name) for name in stable_fields)
        or any(getattr(after, name) != getattr(before, name) for name in stable_fields)
        or file_ops._is_reparse_point(path)
    ):
        raise WorkspaceCompareError("file changed while it was being hashed")
    budget["bytes"] += read_bytes
    return read_bytes, digest.hexdigest()


def _directory_entries(path, budget):
    try:
        before = path.lstat()
        if not stat.S_ISDIR(before.st_mode) or file_ops._is_reparse_point(path):
            raise WorkspaceCompareError("comparison encountered an unsafe directory")
        with os.scandir(path) as iterator:
            names = []
            for entry in iterator:
                _check_deadline(budget)
                names.append(entry.name)
                if len(names) > budget["max_entries"]:
                    raise WorkspaceCompareError(
                        "workspace comparison exceeded the entry ceiling"
                    )
            names.sort()
        after = path.lstat()
    except WorkspaceCompareError:
        raise
    except OSError as exc:
        raise WorkspaceCompareError("directory could not be safely enumerated") from exc
    if (
        not before.st_ino or not after.st_ino
        or _identity(before) != _identity(after)
        or before.st_mtime_ns != after.st_mtime_ns
        or file_ops._is_reparse_point(path)
    ):
        raise WorkspaceCompareError("directory changed during comparison")
    return names, before


def _validate_directory_unchanged(path, names, before, budget):
    current_names, current = _directory_entries(path, budget)
    if (
        current_names != names
        or _identity(current) != _identity(before)
        or current.st_mtime_ns != before.st_mtime_ns
    ):
        raise WorkspaceCompareError("directory contents changed during comparison")


def _inventory(root, authorized, budget):
    inventory = {}
    counts = {"entries": 0, "files": 0, "directories": 0, "bytes_hashed": 0}

    def visit(path, relative):
        _reserve_entry(budget)
        counts["entries"] += 1
        if _sensitive(path, authorized):
            raise WorkspaceCompareError("comparison encountered secret or control state")
        if file_ops._is_reparse_point(path):
            raise WorkspaceCompareError("comparison encountered a symlink or junction")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise WorkspaceCompareError("comparison entry changed during inventory") from exc
        key = relative.as_posix()
        if stat.S_ISREG(metadata.st_mode):
            size, digest = _hash_file(path, budget)
            inventory[key] = {
                "path": key, "type": "file", "size": size, "sha256": digest,
            }
            counts["files"] += 1
            counts["bytes_hashed"] += size
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceCompareError("comparison encountered a special filesystem entry")
        inventory[key] = {
            "path": key, "type": "directory", "size": 0, "sha256": None,
        }
        counts["directories"] += 1
        names, before = _directory_entries(path, budget)
        for name in names:
            visit(path / name, relative / name if key != "." else Path(name))
        _validate_directory_unchanged(path, names, before, budget)

    visit(root, Path("."))
    return inventory, counts


def _encoded(report):
    return json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def encode_result(report):
    return _encoded(report)


def _output_candidate(report, remove_count):
    candidate = dict(report)
    remaining = remove_count
    for name in ("same", "changed", "removed", "added"):
        rows = report[name]
        removed = min(remaining, len(rows))
        candidate[name] = rows[:len(rows) - removed] if removed else list(rows)
        remaining -= removed
    candidate["details_truncated"] = bool(
        report["details_truncated"] or remove_count
    )
    candidate["output_bytes"] = 0
    return candidate


def _stabilize_output_size(report, deadline):
    for _ in range(10):
        _check_deadline({"deadline": deadline})
        encoded = _encoded(report)
        _check_deadline({"deadline": deadline})
        actual = len(encoded.encode("utf-8"))
        if actual == report["output_bytes"]:
            return actual
        report["output_bytes"] = actual
    _check_deadline({"deadline": deadline})
    encoded = _encoded(report)
    _check_deadline({"deadline": deadline})
    return len(encoded.encode("utf-8"))


def _fit_output(report, max_output, deadline):
    """Keep the maximum detail prefix using logarithmic serialization passes."""
    total = sum(len(report[name]) for name in ("added", "removed", "changed", "same"))
    low = 0
    high = total
    best = None
    while low <= high:
        _check_deadline({"deadline": deadline})
        removed = (low + high) // 2
        candidate = _output_candidate(report, removed)
        actual = _stabilize_output_size(candidate, deadline)
        if actual <= max_output and actual == candidate["output_bytes"]:
            best = candidate
            high = removed - 1
        else:
            low = removed + 1
    if best is None:
        raise WorkspaceCompareError("comparison metadata exceeds the output byte ceiling")
    return best


def compare_workspaces(
    left, right, *, max_entries=DEFAULT_MAX_ENTRIES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
    max_details=DEFAULT_MAX_DETAILS, max_output_bytes=DEFAULT_OUTPUT_BYTES,
    timeout=DEFAULT_TIMEOUT_SECONDS, extra_roots="", bypass=False,
    developer_authorized=False,
):
    """Return deterministic metadata differences without exposing file contents."""
    left_root, left_authorized = resolve_compare_root(
        left, extra_roots=extra_roots, bypass=bypass,
        developer_authorized=developer_authorized,
    )
    right_root, right_authorized = resolve_compare_root(
        right, extra_roots=extra_roots, bypass=bypass,
        developer_authorized=developer_authorized,
    )
    limits = {
        "entries": _bounded_int(max_entries, DEFAULT_MAX_ENTRIES, 2, MAX_ENTRIES),
        "file_bytes": _bounded_int(
            max_file_bytes, DEFAULT_MAX_FILE_BYTES, 1, MAX_FILE_BYTES,
        ),
        "total_bytes": _bounded_int(
            max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, 1, MAX_TOTAL_BYTES,
        ),
        "details": _bounded_int(max_details, DEFAULT_MAX_DETAILS, 0, MAX_DETAILS),
        "output_bytes": _bounded_int(
            max_output_bytes, DEFAULT_OUTPUT_BYTES, 1024, MAX_OUTPUT_BYTES,
        ),
        "timeout_seconds": _bounded_timeout(timeout),
    }
    budget = {
        "entries": 0, "bytes": 0, "max_entries": limits["entries"],
        "max_file_bytes": limits["file_bytes"],
        "max_total_bytes": limits["total_bytes"],
        "deadline": time.monotonic() + limits["timeout_seconds"],
    }
    left_inventory, left_counts = _inventory(left_root, left_authorized, budget)
    right_inventory, right_counts = _inventory(right_root, right_authorized, budget)
    added = []
    removed = []
    changed = []
    same = []
    for path in sorted(set(left_inventory) | set(right_inventory)):
        left_row = left_inventory.get(path)
        right_row = right_inventory.get(path)
        if left_row is None:
            added.append(right_row)
        elif right_row is None:
            removed.append(left_row)
        elif left_row == right_row:
            same.append(left_row)
        else:
            changed.append({"path": path, "left": left_row, "right": right_row})
    summary = {
        "added": len(added), "removed": len(removed),
        "changed": len(changed), "same": len(same),
    }
    details = {"added": [], "removed": [], "changed": [], "same": []}
    remaining = limits["details"]
    for name, rows in (
        ("added", added), ("removed", removed),
        ("changed", changed), ("same", same),
    ):
        details[name] = rows[:remaining]
        remaining -= len(details[name])
    report = {
        "ok": True,
        "left": {"root": str(left_root), **left_counts},
        "right": {"root": str(right_root), **right_counts},
        "summary": summary,
        **details,
        "details_truncated": sum(summary.values()) > limits["details"],
        "scan": {"entries": budget["entries"], "bytes_hashed": budget["bytes"]},
        "limits": limits,
        "output_bytes": 0,
    }
    return _fit_output(report, limits["output_bytes"], budget["deadline"])
