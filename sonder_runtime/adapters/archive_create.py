"""Guarded, bounded, transactional ZIP/TAR creation using only stdlib.

This module is the canonical owner of archive creation.  The legacy
``archive_create`` import is an identity redirect to this module so existing
callers and monkeypatches continue to operate on the same implementation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.application.ports.archive_create_limits import ArchiveCreateLimits
from sonder_runtime.application.ports.archive_create import ArchiveCreateRequest


CHUNK_BYTES = 64 * 1024
HARD_MAX_FILES = ArchiveCreateLimits.HARD_MAX_FILES
HARD_MAX_ENTRIES = ArchiveCreateLimits.HARD_MAX_ENTRIES
HARD_MAX_FILE_BYTES = ArchiveCreateLimits.HARD_MAX_FILE_BYTES
HARD_MAX_TOTAL_BYTES = ArchiveCreateLimits.HARD_MAX_TOTAL_BYTES
HARD_MAX_DEPTH = ArchiveCreateLimits.HARD_MAX_DEPTH
HARD_MAX_RESULTS = ArchiveCreateLimits.HARD_MAX_RESULTS
HARD_MAX_PATH_CHARS = 1_024

DEFAULT_MAX_FILES = ArchiveCreateLimits.DEFAULT_MAX_FILES
DEFAULT_MAX_ENTRIES = ArchiveCreateLimits.DEFAULT_MAX_ENTRIES
DEFAULT_MAX_FILE_BYTES = ArchiveCreateLimits.DEFAULT_MAX_FILE_BYTES
DEFAULT_MAX_TOTAL_BYTES = ArchiveCreateLimits.DEFAULT_MAX_TOTAL_BYTES
DEFAULT_MAX_DEPTH = ArchiveCreateLimits.DEFAULT_MAX_DEPTH
DEFAULT_MAX_RESULTS = ArchiveCreateLimits.DEFAULT_MAX_RESULTS


class ArchiveCreateRejected(ValueError):
    """Input or destination violates the archive-creation policy."""


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _reject_linked_request(path: Path, label: str, *, parent_only: bool = False) -> None:
    inspected = path.parent if parent_only else path
    if _is_reparse(inspected):
        raise PermissionError("%s may not be a symlink or junction" % label)
    lexical = os.path.normcase(os.path.normpath(os.path.abspath(str(inspected))))
    physical = os.path.normcase(os.path.normpath(os.path.realpath(str(inspected))))
    if lexical != physical:
        raise PermissionError("%s traverses a symlink or junction" % label)


def _signature(value) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_mode), int(value.st_size), int(value.st_mtime_ns),
        int(getattr(value, "st_dev", 0)), int(getattr(value, "st_ino", 0)),
    )


def _opened_handle_path(fd: int) -> Path:
    if os.name == "nt":
        import msvcrt
        return _windows_handle_path(msvcrt.get_osfhandle(fd))
    for link in ("/proc/self/fd/%d" % fd, "/dev/fd/%d" % fd):
        try:
            value = os.readlink(link)
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise PermissionError("archive input was deleted after preflight")
        return Path(value)
    raise PermissionError("platform cannot validate an opened archive input")


def _windows_handle_path(handle) -> Path:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    needed = function(handle, None, 0, 0)
    if not needed:
        raise OSError(ctypes.get_last_error(), "could not resolve opened filesystem handle")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    if not function(handle, buffer, len(buffer), 0):
        raise OSError(ctypes.get_last_error(), "could not resolve opened filesystem handle")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class _DestinationDirectory:
    """Hold and address the validated destination directory by identity."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None
        self.handle = None

    def __enter__(self):
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateFileW
            create.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            create.restype = ctypes.c_void_p
            # Request the narrowest sharing and expose, rather than follow, a
            # late junction or directory symlink. The named staging handle
            # opened before preflight supplies the Windows rename anchor.
            self.handle = create(
                str(self.path), 0x00000080, 0x00000001 | 0x00000002,
                None, 3, 0x02000000 | 0x00200000, None,
            )
            if self.handle in (None, ctypes.c_void_p(-1).value):
                self.handle = None
                raise OSError(ctypes.get_last_error(), "could not lock archive destination directory")
        else:
            flags = (
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            self.fd = os.open(self.path, flags)
        try:
            self.validate()
        except Exception:
            self._close()
            raise
        return self

    def __exit__(self, _type, _value, _traceback):
        self._close()

    def _close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.handle is not None:
            import ctypes
            close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close.argtypes = [ctypes.c_void_p]
            close.restype = ctypes.c_int
            close(self.handle)
            self.handle = None

    def validate(self) -> None:
        if os.name == "nt":
            opened = _windows_handle_path(self.handle).resolve()
            if opened != self.path or _is_reparse(self.path):
                raise PermissionError("archive destination directory identity changed")
            metadata = self.path.lstat()
        else:
            metadata = os.stat(self.path, follow_symlinks=False)
            opened = os.fstat(self.fd)
            if not os.path.samestat(opened, metadata):
                raise PermissionError("archive destination directory identity changed")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("archive destination parent is not a regular directory")

    def create_stage(self) -> tuple[int, str]:
        self.validate()
        if os.name == "nt":
            descriptor, path = tempfile.mkstemp(
                prefix=".sonder-archive-create-", suffix=".tmp", dir=self.path,
            )
            actual = _opened_handle_path(descriptor)
            if actual.parent.resolve() != self.path:
                os.close(descriptor)
                try:
                    actual.unlink()
                except OSError:
                    pass
                raise PermissionError("archive staging escaped the destination directory")
            return descriptor, Path(path).name
        flags = (
            os.O_RDWR | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(128):
            name = ".sonder-archive-create-%s.tmp" % secrets.token_hex(12)
            try:
                return os.open(name, flags, 0o600, dir_fd=self.fd), name
            except FileExistsError:
                continue
        raise FileExistsError("could not allocate unique archive staging file")

    def stat(self, name: str):
        if os.name == "nt":
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def exists(self, name: str) -> bool:
        try:
            self.stat(name)
            return True
        except FileNotFoundError:
            return False

    def link(self, source: str, destination: str) -> None:
        self.validate()
        if os.name == "nt":
            os.link(
                self.path / source, self.path / destination,
                follow_symlinks=False,
            )
        else:
            os.link(
                source, destination, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                follow_symlinks=False,
            )

    def unlink(self, name: str) -> None:
        if os.name == "nt":
            (self.path / name).unlink()
        else:
            os.unlink(name, dir_fd=self.fd)


def _open_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))

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
        raise OSError(ctypes.get_last_error(), "could not open guarded archive input")
    info = FileAttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    get_info.restype = ctypes.c_int
    if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(raw_handle)
        raise OSError(ctypes.get_last_error(), "could not inspect guarded archive input")
    if info.attributes & 0x00000400:
        kernel32.CloseHandle(raw_handle)
        raise PermissionError("archive input became a symlink or junction")
    try:
        return msvcrt.open_osfhandle(raw_handle, flags)
    except Exception:
        kernel32.CloseHandle(raw_handle)
        raise


@contextlib.contextmanager
def _open_stable_file(row: dict, root: Path, extra_roots: str):
    fd = _open_no_follow(row["source"])
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PermissionError("archive input is not a regular file: %s" % row["path"])
        if _signature(before) != row["signature"]:
            raise ArchiveCreateRejected("archive input changed after preflight: %s" % row["path"])
        actual = file_ops.resolve_repository_read_path(
            str(_opened_handle_path(fd)), allow_workspace_root=False,
            reject_sensitive=True, extra_roots=str(root) + os.pathsep + str(extra_roots or ""),
        )
        current = actual.stat(follow_symlinks=False)
        if not os.path.samestat(before, current):
            raise ArchiveCreateRejected("archive input handle identity changed: %s" % row["path"])
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle
        after = os.fstat(fd)
        if _signature(after) != row["signature"]:
            raise ArchiveCreateRejected("archive input changed while reading: %s" % row["path"])
    finally:
        os.close(fd)


class _DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()
        self.bytes = 0

    def read(self, size=-1):
        chunk = self.source.read(size)
        if chunk:
            self.digest.update(chunk)
            self.bytes += len(chunk)
        return chunk

    def hexdigest(self):
        return self.digest.hexdigest()


def _preflight_digest(row: dict, root: Path, extra_roots: str) -> str:
    with _open_stable_file(row, root, extra_roots) as source:
        reader = _DigestReader(source)
        while reader.read(CHUNK_BYTES):
            pass
    if reader.bytes != row["bytes"]:
        raise ArchiveCreateRejected("archive input size changed during preflight: %s" % row["path"])
    return reader.hexdigest()


def _parse_inputs(value) -> list[str]:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("inputs_json must be a JSON array of paths") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("inputs_json must contain at least one path")
    result = []
    for index, item in enumerate(payload):
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValueError("inputs_json item %d must be a non-empty path" % (index + 1))
        result.append(item.strip())
    return result


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sensitive_relative(relative: Path) -> bool:
    directory_names = {
        str(name).casefold() for name in file_ops.SENSITIVE_READ_DIRECTORIES
    }
    if any(part.casefold() in directory_names for part in relative.parts):
        return True
    lowered = relative.name.casefold()
    return (
        file_ops._is_secret_path(relative)
        or lowered in {str(name).casefold() for name in file_ops.CONTROL_CONFIG_FILES}
        or lowered in {"memory.db", "memory.db-wal", "memory.db-shm"}
    )


def _relative_path(path: Path, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("archive input is outside the selected project root") from exc
    if relative == Path("."):
        return relative
    if len(relative.parts) > HARD_MAX_DEPTH or len(relative.as_posix()) > HARD_MAX_PATH_CHARS:
        raise ArchiveCreateRejected("archive input path exceeds hard path limits: %s" % relative)
    if _sensitive_relative(relative):
        raise PermissionError("archive input is sensitive or control state: %s" % relative)
    return relative


def _resolve_root(root: str, extra_roots: str) -> Path:
    requested = Path(str(root or ".")).expanduser()
    if not requested.is_absolute():
        requested = file_ops.workspace_root() / requested
    _reject_linked_request(requested, "archive root")
    resolved = file_ops.resolve_repository_read_path(
        str(root or "."), allow_workspace_root=True, reject_sensitive=True,
        extra_roots=extra_roots,
    )
    if not resolved.is_dir():
        raise ValueError("archive root is not a directory: %s" % resolved)
    if _is_reparse(resolved):
        raise PermissionError("archive root may not be a symlink or junction")
    return resolved


def _resolve_destination(
    destination: str, root: Path, extra_roots: str, developer_authorized: bool,
) -> Path:
    if not str(destination or "").strip():
        raise ValueError("archive destination is required")
    candidate = Path(str(destination)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    _reject_linked_request(candidate, "archive destination parent", parent_only=True)
    resolved = file_ops.resolve_repository_read_path(
        str(candidate), allow_workspace_root=False, reject_sensitive=True,
        extra_roots=str(root) + os.pathsep + str(extra_roots or ""),
    )
    if not _inside(resolved, root):
        raise PermissionError("archive destination must be inside the selected project root")
    file_ops._require_mutation_access(resolved, developer_authorized)
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError("archive destination already exists; overwrite is not supported")
    if not resolved.parent.is_dir() or _is_reparse(resolved.parent):
        raise PermissionError("archive destination parent must be an existing regular directory")
    return resolved


def _plan(
    root: Path, inputs: list[str], destination: Path, limits: dict,
    extra_roots: str,
) -> dict:
    rows = []
    directories = {}
    exact = set()
    folded = {}
    folded_components = {}
    total_bytes = 0
    file_count = 0
    entry_count = 0
    discovered_count = 0
    selected_directories = []

    def add_row(path: Path, source: Path, kind: str, metadata) -> None:
        nonlocal total_bytes, file_count, entry_count
        relative = _relative_path(path, root)
        name = relative.as_posix()
        if relative == Path("."):
            return
        if len(relative.parts) > limits["max_depth"]:
            raise ArchiveCreateRejected("archive input exceeds max_depth: %s" % name)
        if name in exact:
            raise ArchiveCreateRejected("duplicate or overlapping archive input: %s" % name)
        folded_name = name.casefold()
        if folded_name in folded:
            raise ArchiveCreateRejected(
                "case-colliding archive inputs: %s and %s" % (folded[folded_name], name)
            )
        exact.add(name)
        folded[folded_name] = name
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            prefix_key = prefix.casefold()
            previous = folded_components.get(prefix_key)
            if previous is not None and previous != prefix:
                raise ArchiveCreateRejected(
                    "case-colliding archive path components: %s and %s"
                    % (previous, prefix)
                )
            folded_components[prefix_key] = prefix
        entry_count += 1
        if entry_count > limits["max_entries"]:
            raise ArchiveCreateRejected("archive exceeds max_entries")
        row = {
            "path": name, "source": source, "type": kind,
            "bytes": int(metadata.st_size) if kind == "file" else 0,
            "mode": stat.S_IMODE(metadata.st_mode),
            "mtime": int(metadata.st_mtime), "signature": _signature(metadata),
        }
        if kind == "file":
            file_count += 1
            if file_count > limits["max_files"]:
                raise ArchiveCreateRejected("archive exceeds max_files")
            if row["bytes"] > limits["max_file_bytes"]:
                raise ArchiveCreateRejected("archive input exceeds max_file_bytes: %s" % name)
            total_bytes += row["bytes"]
            if total_bytes > limits["max_total_bytes"]:
                raise ArchiveCreateRejected("archive exceeds max_total_bytes")
        rows.append(row)

    def scan_directory(directory: Path) -> None:
        nonlocal discovered_count
        metadata = directory.lstat()
        if _is_reparse(directory) or not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError("archive directory may not be a symlink or junction: %s" % directory)
        relative = _relative_path(directory, root)
        if relative != Path("."):
            add_row(directory, directory, "directory", metadata)
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    discovered_count += 1
                    if discovered_count > limits["max_entries"]:
                        raise ArchiveCreateRejected("archive exceeds max_entries")
                    entries.append(entry)
        except OSError as exc:
            raise ArchiveCreateRejected("could not scan archive directory: %s" % directory) from exc
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        after_scan = directory.lstat()
        if _signature(after_scan) != _signature(metadata):
            raise ArchiveCreateRejected("archive directory changed during preflight: %s" % directory)
        directories[directory] = {
            "signature": _signature(metadata),
            "names": tuple(entry.name for entry in entries),
        }
        for entry in entries:
            child = Path(entry.path)
            _relative_path(child, root)
            metadata = child.lstat()
            if entry.is_symlink() or _is_reparse(child):
                raise PermissionError("archive input tree contains a symlink or junction: %s" % child)
            if stat.S_ISDIR(metadata.st_mode):
                scan_directory(child)
            elif stat.S_ISREG(metadata.st_mode):
                guarded = file_ops.resolve_repository_read_path(
                    str(child), allow_workspace_root=False, reject_sensitive=True,
                    extra_roots=str(root) + os.pathsep + str(extra_roots or ""),
                )
                add_row(child, guarded, "file", metadata)
            else:
                raise PermissionError("archive input tree contains a special file: %s" % child)

    for raw in inputs:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        _reject_linked_request(candidate, "archive input")
        guarded = file_ops.resolve_repository_read_path(
            str(candidate), allow_workspace_root=True, reject_sensitive=True,
            extra_roots=str(root) + os.pathsep + str(extra_roots or ""),
        )
        _relative_path(guarded, root)
        metadata = guarded.lstat()
        if _is_reparse(guarded):
            raise PermissionError("archive input may not be a symlink or junction: %s" % guarded)
        if stat.S_ISDIR(metadata.st_mode):
            selected_directories.append(guarded)
            scan_directory(guarded)
        elif stat.S_ISREG(metadata.st_mode):
            add_row(guarded, guarded, "file", metadata)
        else:
            raise PermissionError("archive input is not a regular file or directory: %s" % guarded)

    for directory in selected_directories:
        if _inside(destination, directory):
            raise ArchiveCreateRejected("archive destination may not be inside an input directory")
    rows.sort(key=lambda row: row["path"])
    for row in rows:
        if row["type"] == "file":
            row["sha256"] = _preflight_digest(row, root, extra_roots)
    return {
        "root": root, "destination": destination, "rows": rows,
        "directories": directories, "files": file_count,
        "total_bytes": total_bytes, "entry_count": entry_count,
    }


def _revalidate(plan: dict) -> None:
    for directory in sorted(plan["directories"], key=lambda value: str(value)):
        expected = plan["directories"][directory]
        metadata = directory.lstat()
        if _is_reparse(directory) or _signature(metadata) != expected["signature"]:
            raise ArchiveCreateRejected("archive directory changed after preflight")
        with os.scandir(directory) as iterator:
            names = sorted(
                (entry.name for entry in iterator),
                key=lambda name: (name.casefold(), name),
            )
        if tuple(names) != expected["names"]:
            raise ArchiveCreateRejected("archive directory membership changed after preflight")
    for row in plan["rows"]:
        if row["type"] != "file":
            continue
        metadata = row["source"].lstat()
        if _is_reparse(row["source"]) or _signature(metadata) != row["signature"]:
            raise ArchiveCreateRejected("archive input changed after preflight: %s" % row["path"])


def _zip_timestamp(row: dict, deterministic: bool):
    if deterministic:
        return (1980, 1, 1, 0, 0, 0)
    value = time.localtime(max(315532800, row["mtime"]))[:6]
    return (min(2107, max(1980, value[0])), *value[1:])


def _archive_mode(row: dict, deterministic: bool) -> int:
    if row["type"] == "directory":
        return 0o755
    if deterministic:
        return 0o755 if row["mode"] & 0o111 else 0o644
    return row["mode"] & 0o777


def _write_zip(stage, plan: dict, deterministic: bool, extra_roots: str) -> None:
    stage.seek(0)
    stage.truncate(0)
    with zipfile.ZipFile(stage, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for row in plan["rows"]:
            name = row["path"] + ("/" if row["type"] == "directory" else "")
            info = zipfile.ZipInfo(name, _zip_timestamp(row, deterministic))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = _archive_mode(row, deterministic)
            kind = stat.S_IFDIR if row["type"] == "directory" else stat.S_IFREG
            info.external_attr = (kind | mode) << 16
            if row["type"] == "directory":
                archive.writestr(info, b"")
                continue
            with _open_stable_file(row, plan["root"], extra_roots) as source:
                reader = _DigestReader(source)
                with archive.open(info, "w", force_zip64=True) as target:
                    while True:
                        chunk = reader.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        target.write(chunk)
                if reader.bytes != row["bytes"] or reader.hexdigest() != row["sha256"]:
                    raise ArchiveCreateRejected("archive input content changed: %s" % row["path"])


def _write_tar(stage, plan: dict, deterministic: bool, extra_roots: str) -> None:
    stage.seek(0)
    stage.truncate(0)
    with tarfile.open(fileobj=stage, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for row in plan["rows"]:
            info = tarfile.TarInfo(row["path"])
            info.type = tarfile.DIRTYPE if row["type"] == "directory" else tarfile.REGTYPE
            info.size = row["bytes"]
            info.mode = _archive_mode(row, deterministic)
            info.mtime = 0 if deterministic else row["mtime"]
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if row["type"] == "directory":
                archive.addfile(info)
                continue
            with _open_stable_file(row, plan["root"], extra_roots) as source:
                reader = _DigestReader(source)
                archive.addfile(info, reader)
                if reader.bytes != row["bytes"] or reader.hexdigest() != row["sha256"]:
                    raise ArchiveCreateRejected("archive input content changed: %s" % row["path"])


def _sha256(handle) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while True:
        chunk = handle.read(CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _create_archive_native(
    root: str, inputs_json, destination: str, *, archive_format: str = "zip",
    deterministic: bool = True, max_files=DEFAULT_MAX_FILES,
    max_entries=DEFAULT_MAX_ENTRIES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES, max_depth=DEFAULT_MAX_DEPTH,
    max_results=DEFAULT_MAX_RESULTS, extra_roots: str = "",
    developer_authorized: bool = False,
) -> dict:
    archive_format = str(archive_format or "zip").strip().casefold()
    if archive_format not in {"zip", "tar"}:
        raise ValueError("archive_format must be zip or tar")
    limits = ArchiveCreateLimits.from_values(
        max_files, max_entries, max_file_bytes, max_total_bytes, max_depth, max_results,
    ).as_dict()
    resolved_root = _resolve_root(root, extra_roots)
    resolved_destination = _resolve_destination(
        destination, resolved_root, extra_roots, developer_authorized,
    )
    inputs = _parse_inputs(inputs_json)
    destination_name = resolved_destination.name
    with _DestinationDirectory(resolved_destination.parent) as parent:
        descriptor = None
        stage_name = ""
        published = False
        destination_created = False
        destination_signature = None
        try:
            # A named open child prevents destination-parent replacement on
            # Windows. POSIX publication is already rooted at dir_fd, so defer
            # staging there until after the input plan to avoid introducing a
            # temporary member into a directory being scanned.
            if os.name == "nt":
                descriptor, stage_name = parent.create_stage()
            plan = _plan(
                resolved_root, inputs, resolved_destination, limits, extra_roots,
            )
            _revalidate(plan)
            if descriptor is None:
                descriptor, stage_name = parent.create_stage()
            with os.fdopen(descriptor, "w+b", closefd=False) as stage_handle:
                if archive_format == "zip":
                    _write_zip(stage_handle, plan, deterministic is True, extra_roots)
                else:
                    _write_tar(stage_handle, plan, deterministic is True, extra_roots)
                stage_handle.flush()
                os.fsync(descriptor)
                _revalidate(plan)
                digest = _sha256(stage_handle)
                opened_stage = os.fstat(descriptor)
                archive_bytes = opened_stage.st_size
                named_stage = parent.stat(stage_name)
                if not stat.S_ISREG(named_stage.st_mode) or not os.path.samestat(
                    opened_stage, named_stage,
                ):
                    raise ArchiveCreateRejected("archive staging file identity changed")
            if parent.exists(destination_name):
                raise FileExistsError("archive destination appeared during creation")
            # Publish relative to the held directory identity. On Windows the
            # handle denies replacement; on POSIX both names are resolved by
            # dir_fd, so a swapped path or symlink is never followed.
            parent.link(stage_name, destination_name)
            destination_created = True
            destination_metadata = parent.stat(destination_name)
            destination_signature = _signature(destination_metadata)
            if not stat.S_ISREG(destination_metadata.st_mode) or not os.path.samestat(
                os.fstat(descriptor), destination_metadata,
            ):
                raise ArchiveCreateRejected("published archive identity does not match staging")
            parent.validate()
            published = True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if destination_created and not published:
                try:
                    current = parent.stat(destination_name)
                    if _signature(current) == destination_signature:
                        parent.unlink(destination_name)
                except OSError:
                    pass
            if stage_name and parent.exists(stage_name):
                parent.unlink(stage_name)
    rows = [
        {"path": row["path"], "type": row["type"], "bytes": row["bytes"]}
        for row in plan["rows"]
    ]
    omitted = max(0, len(rows) - limits["max_results"])
    return {
        "ok": True, "archive_format": archive_format,
        "deterministic": deterministic is True,
        "root": str(resolved_root), "destination": str(resolved_destination),
        "files": plan["files"],
        "directories": sum(row["type"] == "directory" for row in plan["rows"]),
        "input_bytes": plan["total_bytes"], "archive_bytes": archive_bytes,
        "archive_sha256": digest, "entries": rows[:limits["max_results"]],
        "truncated": bool(omitted), "omitted_results": omitted,
        "limits": limits, "overwrote": False,
    }


def format_result(data: dict) -> str:
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    return rendered.encode("utf-8", errors="backslashreplace").decode("utf-8")


def create_archive(
    root, inputs_json=None, destination=None, *, archive_format: str = "zip",
    deterministic: bool = True, max_files=DEFAULT_MAX_FILES,
    max_entries=DEFAULT_MAX_ENTRIES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES, max_depth=DEFAULT_MAX_DEPTH,
    max_results=DEFAULT_MAX_RESULTS, extra_roots: str = "",
    developer_authorized: bool = False,
) -> dict:
    """Create an archive through either the legacy or typed public API.

    The positional form is the historical root-module API.  Accepting an
    ``ArchiveCreateRequest`` as the sole argument retains the packaged
    adapter's pre-migration API while keeping all policy in this module.
    """
    if isinstance(root, ArchiveCreateRequest):
        if inputs_json is not None or destination is not None:
            raise TypeError("typed archive request cannot include positional arguments")
        request = root
        kwargs = {
            "archive_format": request.archive_format,
            "deterministic": request.deterministic,
            "extra_roots": request.extra_roots,
            "developer_authorized": request.developer_authorized,
        }
        for name in (
            "max_files", "max_entries", "max_file_bytes", "max_total_bytes",
            "max_depth", "max_results",
        ):
            value = getattr(request, name)
            if value is not None:
                kwargs[name] = value
        return _create_archive_native(
            request.root, request.inputs_json, request.destination, **kwargs,
        )
    if inputs_json is None or destination is None:
        raise TypeError("root, inputs_json, and destination are required")
    return _create_archive_native(
        root, inputs_json, destination,
        archive_format=archive_format, deterministic=deterministic,
        max_files=max_files, max_entries=max_entries,
        max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
        max_depth=max_depth, max_results=max_results,
        extra_roots=extra_roots, developer_authorized=developer_authorized,
    )


class ArchiveCreateAdapter:
    """Concrete gateway implementation for the application composition root."""

    def create_archive(self, request: ArchiveCreateRequest) -> dict:
        if not isinstance(request, ArchiveCreateRequest):
            raise TypeError("request must be an ArchiveCreateRequest")
        return create_archive(request)


__all__ = [
    "ArchiveCreateAdapter", "ArchiveCreateRejected", "create_archive",
    "format_result",
]
