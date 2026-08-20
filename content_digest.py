"""Streaming SHA-256 digests for guarded files and repository trees."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import contextlib
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops


ALGORITHM = "sha256"
MERKLE_FORMAT = "sonder-directory-manifest-v1"
CHUNK_BYTES = 64 * 1024

HARD_MAX_FILE_BYTES = 256_000_000
HARD_MAX_TOTAL_BYTES = 256_000_000
HARD_MAX_FILES = 10_000
HARD_MAX_DEPTH = 32
HARD_MAX_RESULTS = 10_000
HARD_MAX_DISCOVERY_ENTRIES = 100_000

DEFAULT_FILE_BYTES = 32_000_000
DEFAULT_TOTAL_BYTES = 32_000_000
DEFAULT_FILES = 2_000
DEFAULT_DEPTH = 12
DEFAULT_RESULTS = 2_500


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(ceiling, parsed))


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _opened_handle_path(fd: int) -> Path:
    """Return the path of the already-open handle, or fail closed."""
    if os.name == "nt":
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        needed = function(handle, None, 0, 0)
        if not needed:
            raise OSError(ctypes.get_last_error(), "could not resolve opened file handle")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not function(handle, buffer, len(buffer), 0):
            raise OSError(ctypes.get_last_error(), "could not resolve opened file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    for link in ("/proc/self/fd/%d" % fd, "/dev/fd/%d" % fd):
        try:
            value = os.readlink(link)
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise PermissionError("opened source was deleted during validation")
        return Path(value)
    raise PermissionError("platform cannot validate an opened file handle")


@contextlib.contextmanager
def _open_guarded_binary(path: Path, extra_roots: str):
    """Open without following a replacement link and validate the handle."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name == "nt":
        import ctypes
        import msvcrt

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        create.restype = ctypes.c_void_p
        raw_handle = create(
            str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x00200000 | 0x08000000, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if raw_handle == invalid:
            raise OSError(ctypes.get_last_error(), "could not open guarded source")
        info = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        get_info.restype = ctypes.c_int
        if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(raw_handle)
            raise OSError(ctypes.get_last_error(), "could not inspect guarded source")
        if info.attributes & 0x00000400:
            kernel32.CloseHandle(raw_handle)
            raise PermissionError("replacement symlink or junction is not hashed")
        try:
            fd = msvcrt.open_osfhandle(raw_handle, flags)
        except Exception:
            kernel32.CloseHandle(raw_handle)
            raise
    else:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("opened source is not a regular file")
        actual = file_ops.resolve_repository_read_path(
            str(_opened_handle_path(fd)), allow_workspace_root=False,
            reject_sensitive=True, extra_roots=extra_roots,
        )
        current = actual.stat(follow_symlinks=False)
        if not os.path.samestat(opened, current):
            raise PermissionError("source changed while validating its opened handle")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def _requested_path(path: str) -> Path:
    candidate = Path(str(path or ".")).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_symlinked_root(path: str, label: str) -> None:
    requested = _requested_path(path)
    if _is_reparse(requested):
        raise PermissionError("%s may not be a symlink or junction" % label)
    lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(requested))))
    physical = os.path.normcase(os.path.normpath(os.path.realpath(str(requested))))
    if lexical != physical:
        raise PermissionError("%s traverses a symlink or junction" % label)


def _stream_sha256(path: Path, max_bytes: int, extra_roots: str = "") -> dict:
    """Hash one stable regular file without reading beyond *max_bytes*."""
    digest = hashlib.sha256()
    with _open_guarded_binary(path, extra_roots) as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > max_bytes:
            return {
                "sha256": None, "bytes": 0, "source_bytes": before.st_size,
                "truncated": True,
                "error": "file exceeds byte ceiling (%d > %d)" % (
                    before.st_size, max_bytes,
                ),
            }
        remaining = before.st_size
        total = 0
        while remaining:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
    stable = (
        total == before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and getattr(before, "st_ino", None) == getattr(after, "st_ino", None)
    )
    if not stable:
        return {
            "sha256": None, "bytes": total, "source_bytes": after.st_size,
            "truncated": True, "error": "file changed while hashing",
        }
    return {
        "sha256": digest.hexdigest(), "bytes": total,
        "source_bytes": before.st_size, "truncated": False, "error": "",
    }


def digest_file(path: str, *, max_bytes: int = DEFAULT_FILE_BYTES, extra_roots: str = "") -> dict:
    limit = _bounded(max_bytes, DEFAULT_FILE_BYTES, HARD_MAX_FILE_BYTES)
    _reject_symlinked_root(path, "file digest path")
    guarded = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=False, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    if not guarded.exists():
        raise FileNotFoundError("digest file not found: %s" % guarded)
    if not guarded.is_file():
        raise ValueError("digest path is not a file: %s" % guarded)
    if _is_reparse(guarded):
        raise PermissionError("file digest path may not be a symlink or junction")
    hashed = _stream_sha256(guarded, limit, extra_roots)
    return {
        "algorithm": ALGORITHM,
        "path": str(guarded),
        "max_bytes": limit,
        **hashed,
    }


def _iter_entries(root: Path, max_depth: int, snapshots: dict[Path, tuple[str, ...]]):
    stack = [(root, "", 0)]
    discovered = 0
    while stack:
        directory, prefix, depth = stack.pop()
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    discovered += 1
                    if discovered > HARD_MAX_DISCOVERY_ENTRIES:
                        yield "limit", "", None, "max_discovery_entries"
                        return
                    entries.append(entry)
            entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
            snapshots[directory] = tuple(entry.name for entry in entries)
        except OSError as exc:
            yield "error", prefix or ".", None, "could not scan directory: %s" % exc
            continue
        children = []
        for entry in entries:
            relative = "%s/%s" % (prefix, entry.name) if prefix else entry.name
            relative = relative.replace("\\", "/")
            child = Path(entry.path)
            try:
                if entry.is_symlink() or _is_reparse(child):
                    yield "error", relative, child, "symlink or junction skipped"
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() in file_ops.SENSITIVE_READ_DIRECTORIES:
                        yield "error", relative, child, "sensitive directory skipped"
                    elif depth >= max_depth:
                        yield "limit", relative, child, "max_depth"
                    else:
                        children.append((child, relative, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield "file", relative, child, ""
            except OSError as exc:
                yield "error", relative, child, "could not inspect entry: %s" % exc
        for item in reversed(children):
            stack.append(item)


def _manifest_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    digest.update((MERKLE_FORMAT + "\n").encode("ascii"))
    for row in sorted(rows, key=lambda item: item["path"]):
        canonical = json.dumps(
            {"bytes": row["bytes"], "path": row["path"], "sha256": row["sha256"]},
            ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        )
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def digest_directory(
    path: str = ".", *, max_depth: int = DEFAULT_DEPTH,
    max_files: int = DEFAULT_FILES,
    max_total_bytes: int = DEFAULT_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_FILE_BYTES,
    max_results: int = DEFAULT_RESULTS,
    extra_roots: str = "",
) -> dict:
    limits = {
        "max_depth": _bounded(max_depth, DEFAULT_DEPTH, HARD_MAX_DEPTH),
        "max_files": _bounded(max_files, DEFAULT_FILES, HARD_MAX_FILES),
        "max_total_bytes": _bounded(
            max_total_bytes, DEFAULT_TOTAL_BYTES, HARD_MAX_TOTAL_BYTES,
        ),
        "max_file_bytes": _bounded(
            max_file_bytes, DEFAULT_FILE_BYTES, HARD_MAX_FILE_BYTES,
        ),
        "max_results": _bounded(max_results, DEFAULT_RESULTS, HARD_MAX_RESULTS),
    }
    _reject_symlinked_root(path, "directory digest root")
    root = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=True, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    if not root.exists():
        raise FileNotFoundError("digest directory not found: %s" % root)
    if not root.is_dir():
        raise ValueError("directory digest path is not a directory: %s" % root)
    if _is_reparse(root):
        raise PermissionError("directory digest root may not be a symlink or junction")

    result = {
        "algorithm": ALGORITHM, "merkle_format": MERKLE_FORMAT,
        "root": str(root), "limits": limits, "files": 0, "bytes": 0,
        "manifest": [], "errors": [], "complete": True,
        "truncated": False, "truncation_reasons": [],
        "merkle_sha256": None, "partial_merkle_sha256": None,
    }

    def incomplete(reason: str, *, truncated: bool) -> None:
        result["complete"] = False
        if truncated:
            result["truncated"] = True
            if reason not in result["truncation_reasons"]:
                result["truncation_reasons"].append(reason)

    def result_room() -> bool:
        if len(result["manifest"]) + len(result["errors"]) < limits["max_results"]:
            return True
        incomplete("max_results", truncated=True)
        return False

    directory_snapshots = {}
    for entry_type, relative, candidate, detail in _iter_entries(
        root, limits["max_depth"], directory_snapshots,
    ):
        if entry_type == "limit":
            incomplete(detail, truncated=True)
            break
        if entry_type == "error":
            if not result_room():
                break
            result["errors"].append({"path": relative, "error": detail})
            incomplete("entry_error", truncated=False)
            continue
        if result["files"] >= limits["max_files"]:
            incomplete("max_files", truncated=True)
            break
        if not result_room():
            break
        result["files"] += 1
        try:
            guarded = file_ops.resolve_repository_read_path(
                str(candidate), allow_workspace_root=False, reject_sensitive=True,
                extra_roots=extra_roots,
            )
            if _is_reparse(candidate):
                raise PermissionError("symlink or junction skipped")
            size = guarded.stat().st_size
            if size > limits["max_file_bytes"]:
                result["errors"].append({
                    "path": relative,
                    "error": "file exceeds max_file_bytes (%d > %d)" % (
                        size, limits["max_file_bytes"],
                    ),
                })
                incomplete("max_file_bytes", truncated=True)
                continue
            remaining = limits["max_total_bytes"] - result["bytes"]
            if size > remaining:
                incomplete("max_total_bytes", truncated=True)
                break
            hashed = _stream_sha256(
                guarded, min(remaining, limits["max_file_bytes"]), extra_roots,
            )
            if hashed["sha256"] is None:
                result["errors"].append({"path": relative, "error": hashed["error"]})
                incomplete("entry_error", truncated=hashed["truncated"])
                continue
            result["bytes"] += hashed["bytes"]
            result["manifest"].append({
                "path": relative, "bytes": hashed["bytes"],
                "sha256": hashed["sha256"],
            })
        except (OSError, PermissionError, ValueError) as exc:
            result["errors"].append({"path": relative, "error": str(exc)})
            incomplete("entry_error", truncated=False)

    for directory in sorted(directory_snapshots, key=lambda item: str(item)):
        relative = "." if directory == root else directory.relative_to(root).as_posix()
        try:
            with os.scandir(directory) as iterator:
                current = sorted(
                    (entry.name for entry in iterator),
                    key=lambda name: (name.casefold(), name),
                )
            if tuple(current) == directory_snapshots[directory]:
                continue
            detail = "directory membership changed while hashing"
        except OSError as exc:
            detail = "could not revalidate directory membership: %s" % exc
        incomplete("directory_changed", truncated=False)
        if result_room():
            result["errors"].append({"path": relative, "error": detail})

    result["manifest"].sort(key=lambda row: row["path"])
    result["errors"].sort(key=lambda row: (row["path"], row["error"]))
    partial = _manifest_digest(result["manifest"])
    result["partial_merkle_sha256"] = partial
    if result["complete"]:
        result["merkle_sha256"] = partial
    return result


def format_digest(data: dict) -> str:
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    # Preserve ordinary Unicode for humans while escaping POSIX surrogateescape
    # code points into valid, transport-safe JSON sequences.
    return rendered.encode("utf-8", errors="backslashreplace").decode("utf-8")
