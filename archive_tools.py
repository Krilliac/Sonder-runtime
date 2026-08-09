"""Guarded ZIP/TAR inspection and transactional extraction using only stdlib."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import file_ops


CHUNK_BYTES = 64 * 1024
HARD_MAX_ENTRIES = 10_000
HARD_MAX_FILE_BYTES = 256_000_000
HARD_MAX_TOTAL_BYTES = 1_000_000_000
HARD_MAX_RATIO = 1_000.0
HARD_MAX_PATH_DEPTH = 128
HARD_MAX_RESULTS = 10_000
HARD_MAX_SECONDS = 60.0
HARD_MAX_PATH_CHARS = 1_024

DEFAULT_MAX_ENTRIES = 2_000
DEFAULT_MAX_FILE_BYTES = 64_000_000
DEFAULT_MAX_TOTAL_BYTES = 256_000_000
DEFAULT_MAX_RATIO = 100.0
DEFAULT_MAX_PATH_DEPTH = 32
DEFAULT_MAX_RESULTS = 2_500
DEFAULT_MAX_SECONDS = 10.0

NESTED_ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
    ".tar.xz", ".txz",
)
WINDOWS_DEVICES = frozenset({
    "con", "prn", "aux", "nul",
    *("com%d" % index for index in range(1, 10)),
    *("lpt%d" % index for index in range(1, 10)),
})


class ArchiveRejected(ValueError):
    """Archive metadata violates the extraction policy."""


def _bounded_int(value, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(ceiling, parsed))


def _bounded_float(value, default: float, ceiling: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1.0, min(ceiling, parsed))


def _limits(
    max_entries, max_file_bytes, max_total_bytes, max_ratio,
    max_path_depth, max_results, max_seconds,
):
    return {
        "max_entries": _bounded_int(max_entries, DEFAULT_MAX_ENTRIES, HARD_MAX_ENTRIES),
        "max_file_bytes": _bounded_int(
            max_file_bytes, DEFAULT_MAX_FILE_BYTES, HARD_MAX_FILE_BYTES,
        ),
        "max_total_bytes": _bounded_int(
            max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, HARD_MAX_TOTAL_BYTES,
        ),
        "max_ratio": _bounded_float(max_ratio, DEFAULT_MAX_RATIO, HARD_MAX_RATIO),
        "max_path_depth": _bounded_int(
            max_path_depth, DEFAULT_MAX_PATH_DEPTH, HARD_MAX_PATH_DEPTH,
        ),
        "max_results": _bounded_int(max_results, DEFAULT_MAX_RESULTS, HARD_MAX_RESULTS),
        "max_seconds": _bounded_float(max_seconds, DEFAULT_MAX_SECONDS, HARD_MAX_SECONDS),
    }


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _requested_path(path: str) -> Path:
    candidate = Path(str(path or "")).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_linked_path(path: str, label: str, *, parent_only: bool = False) -> None:
    requested = _requested_path(path)
    inspected = requested.parent if parent_only else requested
    if _is_reparse(inspected):
        raise PermissionError("%s may not be a symlink or junction" % label)
    lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(inspected))))
    physical = os.path.normcase(os.path.normpath(os.path.realpath(str(inspected))))
    if lexical != physical:
        raise PermissionError("%s traverses a symlink or junction" % label)


def _guarded_source(path: str, extra_roots: str) -> Path:
    _reject_linked_path(path, "archive source")
    source = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=False, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    if not source.exists():
        raise FileNotFoundError("archive source not found: %s" % source)
    if not source.is_file():
        raise ValueError("archive source is not a file: %s" % source)
    if _is_reparse(source):
        raise PermissionError("archive source may not be a symlink or junction")
    return source


def _guarded_destination(
    path: str, extra_roots: str, *, developer_authorized: bool,
) -> Path:
    if not str(path or "").strip():
        raise ValueError("archive destination is required")
    _reject_linked_path(path, "archive destination parent", parent_only=True)
    destination = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=False, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    file_ops._require_mutation_access(destination, developer_authorized)
    if destination.exists():
        raise FileExistsError(
            "archive destination already exists; overwrite is not supported: %s"
            % destination
        )
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            "archive destination parent must already exist: %s" % destination.parent
        )
    if _is_reparse(destination.parent):
        raise PermissionError("archive destination parent may not be a symlink or junction")
    return destination


def _portable_member_path(raw_name: str, *, is_directory: bool, max_depth: int) -> str:
    if not isinstance(raw_name, str) or not raw_name:
        raise ArchiveRejected("archive entry has an empty path")
    if "\x00" in raw_name or "\\" in raw_name:
        raise ArchiveRejected("archive entry uses NUL or backslash path syntax: %r" % raw_name)
    name = raw_name[:-1] if is_directory and raw_name.endswith("/") else raw_name
    if not name or len(name) > HARD_MAX_PATH_CHARS:
        raise ArchiveRejected("archive entry path is empty or too long: %r" % raw_name)
    windows = PureWindowsPath(name)
    posix = PurePosixPath(name)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.anchor:
        raise ArchiveRejected("archive entry path is absolute, drive-qualified, or UNC: %r" % raw_name)
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveRejected("archive entry path contains traversal or empty components: %r" % raw_name)
    if len(parts) > max_depth:
        raise ArchiveRejected("archive entry exceeds path-depth ceiling: %r" % raw_name)
    for part in parts:
        if any(ord(char) < 32 for char in part) or ":" in part:
            raise ArchiveRejected("archive entry contains control or colon syntax: %r" % raw_name)
        if part.rstrip(" .") != part:
            raise ArchiveRejected("archive entry has a trailing dot/space component: %r" % raw_name)
        stem = part.split(".", 1)[0].casefold()
        if stem in WINDOWS_DEVICES:
            raise ArchiveRejected("archive entry uses a reserved device name: %r" % raw_name)
    normalized = "/".join(parts)
    candidate = Path(*parts)
    sensitive_directories = {
        str(name).casefold()
        for name in getattr(file_ops, "SENSITIVE_READ_DIRECTORIES", ())
    }
    if any(part.casefold() in sensitive_directories for part in parts):
        raise ArchiveRejected("archive entry targets a sensitive directory: %s" % normalized)
    if file_ops._is_secret_path(candidate):
        raise ArchiveRejected("archive entry targets a sensitive file: %s" % normalized)
    control_files = {
        str(name).casefold() for name in getattr(file_ops, "CONTROL_CONFIG_FILES", ())
    }
    if candidate.name.casefold() in control_files:
        raise ArchiveRejected("archive entry targets a protected control file: %s" % normalized)
    lowered = normalized.casefold()
    if not is_directory and lowered.endswith(NESTED_ARCHIVE_SUFFIXES):
        raise ArchiveRejected("nested archive entry is not allowed: %s" % normalized)
    return normalized


def _zip_entry(info: zipfile.ZipInfo, limits: dict) -> dict:
    is_directory = info.is_dir()
    path = _portable_member_path(
        info.filename, is_directory=is_directory,
        max_depth=limits["max_path_depth"],
    )
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.flag_bits & 0x1:
        raise ArchiveRejected("encrypted ZIP entry is not allowed: %s" % path)
    if file_type == stat.S_IFLNK:
        raise ArchiveRejected("ZIP symlink entry is not allowed: %s" % path)
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveRejected("ZIP special/device entry is not allowed: %s" % path)
    if file_type == stat.S_IFDIR and not is_directory:
        raise ArchiveRejected("ZIP directory metadata disagrees with its path: %s" % path)
    if file_type == stat.S_IFREG and is_directory:
        raise ArchiveRejected("ZIP file metadata disagrees with its path: %s" % path)
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise ArchiveRejected("ZIP special permission metadata is not allowed: %s" % path)
    size = int(info.file_size)
    compressed = int(info.compress_size)
    if size < 0 or compressed < 0:
        raise ArchiveRejected("ZIP entry has invalid sizes: %s" % path)
    if not is_directory and size > limits["max_file_bytes"]:
        raise ArchiveRejected("entry exceeds per-file byte ceiling: %s" % path)
    ratio = float(size) / float(max(1, compressed))
    if not is_directory and ratio > limits["max_ratio"]:
        raise ArchiveRejected("entry exceeds compression-ratio ceiling: %s" % path)
    return {
        "path": path, "type": "directory" if is_directory else "file",
        "bytes": 0 if is_directory else size,
        "compressed_bytes": 0 if is_directory else compressed,
        "ratio": 0.0 if is_directory else round(ratio, 3),
        "source_index": int(info.header_offset),
    }


def _tar_entry(info: tarfile.TarInfo, limits: dict) -> dict:
    is_directory = info.isdir()
    path = _portable_member_path(
        info.name, is_directory=is_directory,
        max_depth=limits["max_path_depth"],
    )
    if info.issym() or info.islnk():
        raise ArchiveRejected("TAR symlink/hardlink entry is not allowed: %s" % path)
    if not (info.isfile() or is_directory):
        raise ArchiveRejected("TAR special/device entry is not allowed: %s" % path)
    if getattr(info, "sparse", None) or info.pax_headers:
        raise ArchiveRejected("TAR sparse/PAX special metadata is not allowed: %s" % path)
    if int(info.mode or 0) & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise ArchiveRejected("TAR special permission metadata is not allowed: %s" % path)
    size = int(info.size)
    if size < 0:
        raise ArchiveRejected("TAR entry has an invalid size: %s" % path)
    if not is_directory and size > limits["max_file_bytes"]:
        raise ArchiveRejected("entry exceeds per-file byte ceiling: %s" % path)
    return {
        "path": path, "type": "directory" if is_directory else "file",
        "bytes": 0 if is_directory else size,
        "compressed_bytes": None, "ratio": None,
        "source_index": int(info.offset),
    }


def _validate_entries(entries: list[dict], limits: dict, source_bytes: int) -> None:
    if len(entries) > limits["max_entries"]:
        raise ArchiveRejected("archive exceeds entry ceiling")
    total = sum(row["bytes"] for row in entries)
    if total > limits["max_total_bytes"]:
        raise ArchiveRejected("archive exceeds aggregate byte ceiling")
    if total and float(total) / float(max(1, source_bytes)) > limits["max_ratio"]:
        raise ArchiveRejected("archive exceeds aggregate compression-ratio ceiling")
    exact = {}
    folded = {}
    folded_types = {}
    folded_components = {}
    for row in entries:
        path = row["path"]
        if path in exact:
            raise ArchiveRejected("duplicate archive entry path: %s" % path)
        key = path.casefold()
        if key in folded:
            raise ArchiveRejected(
                "case-colliding archive entries: %s and %s" % (folded[key], path)
            )
        exact[path] = path
        folded[key] = path
        folded_types[key] = row["type"]
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            prefix_key = prefix.casefold()
            previous = folded_components.get(prefix_key)
            if previous is not None and previous != prefix:
                raise ArchiveRejected(
                    "case-colliding archive path components: %s and %s"
                    % (previous, prefix)
                )
            folded_components[prefix_key] = prefix
    for path in exact:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if folded_types.get(parent.casefold()) == "file":
                raise ArchiveRejected("file entry is an ancestor of another entry: %s" % parent)


def _archive_kind(source: Path) -> str:
    if zipfile.is_zipfile(source):
        return "zip"
    if tarfile.is_tarfile(source):
        return "tar"
    raise ArchiveRejected("source is not a supported ZIP or TAR archive")


def _stat_signature(source: Path) -> tuple[int, int, int, int]:
    value = source.stat()
    return (
        int(value.st_size), int(value.st_mtime_ns),
        int(getattr(value, "st_dev", 0)), int(getattr(value, "st_ino", 0)),
    )


def _require_same_source(source: Path, signature: tuple[int, int, int, int]) -> None:
    if _stat_signature(source) != signature:
        raise ArchiveRejected("archive source changed after prevalidation")


def _plan(source: Path, limits: dict, *, deadline: float | None = None) -> dict:
    started = time.monotonic()
    deadline = deadline or (started + limits["max_seconds"])
    signature = _stat_signature(source)
    kind = _archive_kind(source)
    if time.monotonic() > deadline:
        raise ArchiveRejected("archive prevalidation exceeded time ceiling")
    entries = []
    if kind == "zip":
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            if len(infos) > limits["max_entries"]:
                raise ArchiveRejected("archive exceeds entry ceiling")
            for info in infos:
                if time.monotonic() > deadline:
                    raise ArchiveRejected("archive prevalidation exceeded time ceiling")
                entries.append(_zip_entry(info, limits))
    else:
        with tarfile.open(source, "r:*") as archive:
            if archive.pax_headers:
                raise ArchiveRejected("TAR global PAX special metadata is not allowed")
            for info in archive:
                if time.monotonic() > deadline:
                    raise ArchiveRejected("archive prevalidation exceeded time ceiling")
                entries.append(_tar_entry(info, limits))
                if len(entries) > limits["max_entries"]:
                    raise ArchiveRejected("archive exceeds entry ceiling")
    _require_same_source(source, signature)
    _validate_entries(entries, limits, source.stat().st_size)
    entries.sort(key=lambda row: row["path"])
    return {
        "archive_type": kind, "source": str(source), "limits": limits,
        "entry_count": len(entries),
        "total_bytes": sum(row["bytes"] for row in entries),
        "entries": entries, "valid": True, "errors": [],
        "source_signature": signature,
    }


def list_archive(
    path: str, *, max_entries=DEFAULT_MAX_ENTRIES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES, max_ratio=DEFAULT_MAX_RATIO,
    max_path_depth=DEFAULT_MAX_PATH_DEPTH, max_results=DEFAULT_MAX_RESULTS,
    max_seconds=DEFAULT_MAX_SECONDS, extra_roots: str = "",
) -> dict:
    limits = _limits(
        max_entries, max_file_bytes, max_total_bytes, max_ratio,
        max_path_depth, max_results, max_seconds,
    )
    source = _guarded_source(path, extra_roots)
    try:
        result = _plan(source, limits)
    except (ArchiveRejected, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return {
            "archive_type": "unknown", "source": str(source), "limits": limits,
            "entry_count": 0, "total_bytes": 0, "entries": [],
            "valid": False, "errors": [str(exc)], "truncated": False,
        }
    result.pop("source_signature", None)
    omitted = max(0, len(result["entries"]) - limits["max_results"])
    result["entries"] = result["entries"][:limits["max_results"]]
    result["truncated"] = bool(omitted)
    result["omitted_results"] = omitted
    return result


def _copy_stream(source, target: Path, expected: int, budget: dict, deadline: float) -> None:
    written = 0
    with target.open("xb") as output:
        while written < expected:
            if time.monotonic() > deadline:
                raise TimeoutError("archive extraction exceeded time ceiling")
            chunk = source.read(min(CHUNK_BYTES, expected - written))
            if not chunk:
                break
            written += len(chunk)
            budget["bytes"] += len(chunk)
            if written > budget["max_file"] or budget["bytes"] > budget["max_total"]:
                raise ArchiveRejected("archive extraction exceeded byte ceiling")
            output.write(chunk)
        if source.read(1):
            raise ArchiveRejected("archive entry produced more bytes than declared")
    if written != expected:
        raise ArchiveRejected("archive entry produced fewer bytes than declared")


def _extract_zip(source: Path, stage: Path, plan: dict, deadline: float) -> None:
    budget = {
        "bytes": 0, "max_file": plan["limits"]["max_file_bytes"],
        "max_total": plan["limits"]["max_total_bytes"],
    }
    by_offset = {row["source_index"]: row for row in plan["entries"]}
    with zipfile.ZipFile(source, "r") as archive:
        for info in archive.infolist():
            row = by_offset[info.header_offset]
            target = stage.joinpath(*row["path"].split("/"))
            if row["type"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as input_stream:
                _copy_stream(input_stream, target, row["bytes"], budget, deadline)


def _extract_tar(source: Path, stage: Path, plan: dict, deadline: float) -> None:
    budget = {
        "bytes": 0, "max_file": plan["limits"]["max_file_bytes"],
        "max_total": plan["limits"]["max_total_bytes"],
    }
    by_offset = {row["source_index"]: row for row in plan["entries"]}
    with tarfile.open(source, "r:*") as archive:
        for info in archive:
            row = by_offset[info.offset]
            target = stage.joinpath(*row["path"].split("/"))
            if row["type"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            input_stream = archive.extractfile(info)
            if input_stream is None:
                raise ArchiveRejected("TAR file entry has no readable payload: %s" % row["path"])
            with input_stream:
                _copy_stream(input_stream, target, row["bytes"], budget, deadline)


def _verify_stage(stage: Path, plan: dict, deadline: float) -> None:
    expected = {
        row["path"]: (row["type"], row["bytes"])
        for row in plan["entries"]
    }
    for path in tuple(expected):
        parts = path.split("/")
        for index in range(1, len(parts)):
            expected.setdefault("/".join(parts[:index]), ("directory", 0))
    actual = {}
    for root, directories, files in os.walk(stage, topdown=True, followlinks=False):
        if time.monotonic() > deadline:
            raise TimeoutError("archive validation exceeded time ceiling")
        root_path = Path(root)
        directories.sort()
        files.sort()
        for name in directories:
            target = root_path / name
            if _is_reparse(target):
                raise ArchiveRejected("extracted directory is a link or reparse point")
            relative = target.relative_to(stage).as_posix()
            actual[relative] = ("directory", 0)
        for name in files:
            target = root_path / name
            if _is_reparse(target) or not target.is_file():
                raise ArchiveRejected("extracted file is not a regular file")
            relative = target.relative_to(stage).as_posix()
            actual[relative] = ("file", target.stat().st_size)
    if actual != expected:
        raise ArchiveRejected("staged extraction does not match the prevalidated manifest")


def extract_archive(
    source_path: str, destination_path: str, *,
    max_entries=DEFAULT_MAX_ENTRIES, max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES, max_ratio=DEFAULT_MAX_RATIO,
    max_path_depth=DEFAULT_MAX_PATH_DEPTH, max_results=DEFAULT_MAX_RESULTS,
    max_seconds=DEFAULT_MAX_SECONDS, extra_roots: str = "",
    developer_authorized: bool = False,
) -> dict:
    limits = _limits(
        max_entries, max_file_bytes, max_total_bytes, max_ratio,
        max_path_depth, max_results, max_seconds,
    )
    started = time.monotonic()
    deadline = started + limits["max_seconds"]
    source = _guarded_source(source_path, extra_roots)
    destination = _guarded_destination(
        destination_path, extra_roots,
        developer_authorized=developer_authorized,
    )
    plan = _plan(source, limits, deadline=deadline)
    stage = Path(tempfile.mkdtemp(prefix=".sonder-archive-", dir=destination.parent))
    promoted = False
    try:
        _require_same_source(source, plan["source_signature"])
        if plan["archive_type"] == "zip":
            _extract_zip(source, stage, plan, deadline)
        else:
            _extract_tar(source, stage, plan, deadline)
        _require_same_source(source, plan["source_signature"])
        _verify_stage(stage, plan, deadline)
        if destination.exists():
            raise FileExistsError("archive destination appeared during extraction")
        stage.rename(destination)
        promoted = True
    finally:
        if not promoted and stage.exists():
            shutil.rmtree(stage)
    return {
        "ok": True, "validation_passed": True,
        "archive_type": plan["archive_type"], "source": str(source),
        "destination": str(destination), "entries": plan["entry_count"],
        "files": sum(row["type"] == "file" for row in plan["entries"]),
        "directories": sum(row["type"] == "directory" for row in plan["entries"]),
        "bytes": plan["total_bytes"], "overwrote": False,
    }


def format_result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
