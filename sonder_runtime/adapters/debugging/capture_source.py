"""Guarded access to crash dumps, cores, traces and profiler captures.

Every capture is hostile input. This adapter reuses the reviewed guards
instead of adding new ones:

- ``log_inspect.resolve_log_path``: allowed roots, sensitive-path refusal,
  reparse components refused;
- ``log_inspect.open_guarded_binary``: no-follow open, regular file, the
  opened handle's path re-resolved and identity re-checked, a change during
  the read refused;
- the -4 credential-store and secret-name refusal
  (``file_ops.credential_read_component`` and ``diagnostics.sources``).

A ``FileByteReader`` gives lane A's pure readers exact-length random access
(``os.pread`` on POSIX; seek+read under a lock on Windows, which has no
``pread``). ``IdentityCache`` memoizes the streamed sha256 by
``(dev, ino, size, mtime_ns)`` for ten minutes, so the permission
evaluator's plan and the executor's plan hash a capture once.

Staging (``stage``) copies captures of at most 512 MiB into the private run
directory (0600), hardlinks larger ones on the state directory's device (a
rename swap then cannot redirect the debugger; content changes are caught by
the post-run identity check) and otherwise passes the canonical path. On
Windows a handle that shares read only is held for the run, so writers and
deleters are denied.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from ...application.debugging.ports import (
    CAPTURE_REJECTED,
    CAPTURE_TOO_LARGE,
    INPUT_CHANGED,
    CaptureIdentity,
    debug_error,
)
from ..diagnostics.sources import _secret_name
from ..filesystem import file_ops
from ..inspection import log_inspect

GIB = 1 << 30
MIB = 1 << 20
COPY_STAGING_MAX_BYTES = 512 * MIB
HASH_MAX_SECONDS = 5.0
HASH_MAX_BYTES = 8 * GIB
HASH_PARTIAL_BYTES = 256 * MIB
IDENTITY_TTL_SECONDS = 600.0
IDENTITY_CACHE_ENTRIES = 256
SNIFF_HEAD_BYTES = 64 * 1024
MAX_DIRECTORY_FILES = 64
_CHUNK = 1 << 20

# Size caps by capture kind (security section 2).
_SIZE_CAPS = {
    "windows_minidump": 8 * GIB, "breakpad_minidump": 8 * GIB, "crashpad_minidump": 8 * GIB,
    "elf_core": 8 * GIB,
    "perf_data": 4 * GIB, "etw_etl": 4 * GIB, "tracy_capture": 4 * GIB, "heaptrack_capture": 4 * GIB,
    "chrome_trace": 1 * GIB,
}
TEXT_CAP_BYTES = 64 * MIB

_MD_CRASHPAD_INFO = 0x43500001
_MD_BREAKPAD_INFO = 0x47670001
_MD_LINUX_FIRST = 0x47670003
_MD_LINUX_LAST = 0x4767000F
_SANITIZER_MARKERS = (
    b"ERROR: AddressSanitizer", b"ERROR: HWAddressSanitizer", b"WARNING: ThreadSanitizer",
    b"ERROR: LeakSanitizer", b"WARNING: MemorySanitizer", b"ERROR: MemorySanitizer",
    b"runtime error:", b"ERROR: ThreadSanitizer",
)
_PLAIN_SUFFIX = re.compile(r"\.[A-Za-z0-9_-]{1,15}")
_TRACY_MAGICS = (b"tlZ\x04", b"tZst", b"tr\xfdP")


def _range_error(detail: str) -> Exception:
    """Lane A's ``ByteRangeError`` (its readers catch it), or ValueError."""
    try:
        from ...domain.binaries.reader import ByteRangeError
    except ImportError:  # pragma: no cover - lane A always ships with this lane
        return ValueError(detail)
    return ByteRangeError(detail)


def size_cap(kind: str) -> int:
    return _SIZE_CAPS.get(kind, TEXT_CAP_BYTES)


# -- byte reader ---------------------------------------------------------------


class FileByteReader:
    """Exact-length reads at an offset from an open file.

    ``read`` returns exactly ``length`` bytes or raises ``ByteRangeError``;
    ``bytes_read`` counts what was pulled (the directory triage budget).
    """

    def __init__(self, handle, size: int, *, closer: Callable[[], None] | None = None,
                 use_pread: bool | None = None) -> None:
        self._handle = handle
        self._size = int(size)
        self._closer = closer
        self._lock = threading.Lock()
        self._pread = (hasattr(os, "pread") and os.name != "nt") if use_pread is None else use_pread
        self._fd = handle.fileno() if self._pread else None
        self.bytes_read = 0
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        if self._closed:
            raise _range_error("reader is closed")
        if (
            not isinstance(offset, int) or not isinstance(length, int)
            or offset < 0 or length < 0 or length > self._size or offset > self._size - length
        ):
            raise _range_error("range %r+%r outside %d bytes" % (offset, length, self._size))
        if length == 0:
            return b""
        if self._pread:
            parts = []
            got = 0
            while got < length:
                chunk = os.pread(self._fd, length - got, offset + got)
                if not chunk:
                    break
                parts.append(chunk)
                got += len(chunk)
            data = b"".join(parts)
        else:
            with self._lock:
                self._handle.seek(offset)
                data = self._handle.read(length)
        if len(data) != length:
            raise _range_error("short read at %d" % offset)
        self.bytes_read += length
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._closer is not None:
            self._closer()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# -- identity ------------------------------------------------------------------


class IdentityCache:
    """sha256 by ``(dev, ino, size, mtime_ns)``, kept ten minutes."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = IDENTITY_TTL_SECONDS,
                 max_entries: int = IDENTITY_CACHE_ENTRIES) -> None:
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._items: dict[tuple[int, int, int, int], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[int, int, int, int]) -> str | None:
        now = self._clock()
        with self._lock:
            found = self._items.get(key)
            if found is None or now - found[1] > self._ttl:
                self._items.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return found[0]

    def put(self, key: tuple[int, int, int, int], sha: str) -> None:
        now = self._clock()
        with self._lock:
            self._items[key] = (sha, now)
            if len(self._items) > self._max:
                for old in sorted(self._items, key=lambda item: self._items[item][1])[
                        : len(self._items) - self._max]:
                    self._items.pop(old, None)


def stream_sha256(handle, size: int, *, clock: Callable[[], float] = time.monotonic,
                  max_seconds: float = HASH_MAX_SECONDS, max_bytes: int = HASH_MAX_BYTES) -> str:
    """Full sha256, or ``partial:<sha256 of the first 256 MiB>+<size>`` past the budget."""
    deadline = clock() + max_seconds
    full = hashlib.sha256()
    head = hashlib.sha256()
    offset = 0
    handle.seek(0)
    while True:
        if offset >= max_bytes or clock() > deadline:
            if offset < HASH_PARTIAL_BYTES:
                while offset < min(size, HASH_PARTIAL_BYTES):
                    chunk = handle.read(min(_CHUNK, HASH_PARTIAL_BYTES - offset))
                    if not chunk:
                        break
                    head.update(chunk)
                    offset += len(chunk)
            return "partial:%s+%d" % (head.hexdigest(), size)
        chunk = handle.read(_CHUNK)
        if not chunk:
            break
        full.update(chunk)
        if offset < HASH_PARTIAL_BYTES:
            head.update(chunk[: HASH_PARTIAL_BYTES - offset])
        offset += len(chunk)
    return full.hexdigest()


# -- sniffing ------------------------------------------------------------------


def _minidump_kind(reader) -> str:
    try:
        header = reader.read(0, 32)
        _sig, _ver, count, rva = struct.unpack_from("<4sIII", header, 0)
        count = min(int(count), 128)
        if count * 12 > reader.size or rva > reader.size - count * 12:
            return "windows_minidump"
        directory = reader.read(rva, count * 12) if count else b""
    except (ValueError, struct.error, OSError, Exception):
        return "windows_minidump"
    kinds = [struct.unpack_from("<I", directory, index * 12)[0] for index in range(count)]
    if _MD_CRASHPAD_INFO in kinds:
        return "crashpad_minidump"
    if _MD_BREAKPAD_INFO in kinds or any(_MD_LINUX_FIRST <= kind <= _MD_LINUX_LAST for kind in kinds):
        return "breakpad_minidump"
    return "windows_minidump"


def _mostly_binary(head: bytes) -> bool:
    sample = head[:512]
    if not sample:
        return False
    text = sum(1 for byte in sample if byte in (9, 10, 13) or 32 <= byte < 127 or byte >= 0x80)
    return text < len(sample) * 0.85


def _looks_folded(text: str) -> bool:
    lines = [line for line in text.splitlines()[:20] if line.strip() and not line.startswith("#")]
    if not lines:
        return False
    good = 0
    for line in lines:
        stack, _, count = line.rstrip().rpartition(" ")
        if stack and count.isdigit():
            good += 1
    return good == len(lines)


def sniff_kind(head: bytes, name: str = "", reader=None) -> str:
    """Classify a capture by magic and markers (``unknown`` when nothing matches)."""
    lower = str(name or "").lower()
    if head[:4] == b"MDMP":
        return _minidump_kind(reader) if reader is not None else "windows_minidump"
    if head[:4] == b"\x7fELF" and len(head) >= 18:
        endian = "<" if head[5:6] == b"\x01" else ">"
        (e_type,) = struct.unpack_from(endian + "H", head, 16)
        return "elf_core" if e_type == 4 else "unknown"
    if head[:8] == b"PERFILE2":
        return "perf_data"
    if head[:4] in _TRACY_MAGICS or lower.endswith(".tracy"):
        return "tracy_capture"
    if lower.endswith(".etl"):
        return "etw_etl"
    if head[:4] == b"\x28\xb5\x2f\xfd" or head[:2] == b"\x1f\x8b":
        return "heaptrack_capture" if "heaptrack" in lower else "unknown"
    if head[:1] == b"\x0a" and _mostly_binary(head[1:]):
        return "perfetto_protobuf"
    if _mostly_binary(head):
        return "unknown"
    text = head.decode("utf-8", "replace").lstrip("﻿")
    stripped = text.lstrip()
    if "<valgrindoutput" in text[:4096]:
        return "valgrind_xml"
    first_line = stripped.split("\n", 1)[0]
    if stripped.startswith("{") and '"bug_type"' in first_line:
        return "apple_ips"
    if stripped.startswith(("{", "[")):
        if '"traceEvents"' in text or '"ph"' in text:
            return "chrome_trace"
        return "unknown"
    if any(marker in head for marker in _SANITIZER_MARKERS):
        return "sanitizer_report"
    if (stripped.startswith("# callgrind format") or "\ncreator: callgrind" in text[:4096]
            or (lower.startswith("callgrind.out") and "events:" in text[:8192])):
        return "callgrind"
    if "MOST CALLS TO ALLOCATION FUNCTIONS" in text or "PEAK MEMORY CONSUMERS" in text:
        return "heaptrack_text"
    if stripped.startswith("# To display the perf.data header info") or (
            stripped.startswith("#") and ("# Samples:" in text or "# Overhead" in text)):
        return "perf_text"
    if "," in first_line:
        header = first_line.replace('"', "").strip().lower()
        if header.startswith("name,src_file") or header.startswith("name,src_line"):
            return "tracy_csv"
        return "profile_csv"
    if _looks_folded(text):
        return "perf_text"
    return "unknown"


# -- the source ---------------------------------------------------------------


def _reject(message: str) -> Exception:
    return debug_error(CAPTURE_REJECTED, message)


def refuse_secret(target: Path) -> None:
    if file_ops.credential_read_component(target):
        raise _reject("credential stores are not read")
    if _secret_name(target.name):
        raise _reject("secret files are not read")


def _display_label(target: Path) -> str:
    for root in (file_ops.workspace_root(),):
        try:
            return target.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
    return target.name


class GuardedCaptureSource:
    """``CaptureSource`` over the reviewed log-inspection guards."""

    def __init__(self, *, identity_cache: IdentityCache | None = None,
                 resolver: Callable[..., Path] | None = None,
                 opener: Callable[..., Any] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cache = identity_cache or IdentityCache(clock=clock)
        self._resolver = resolver or log_inspect.resolve_log_path
        self._opener = opener or log_inspect.open_guarded_binary
        self._clock = clock

    @property
    def identity_cache(self) -> IdentityCache:
        return self._cache

    # -- files -----------------------------------------------------------

    def _resolve(self, path: str, extra_roots: str) -> Path:
        try:
            target = self._resolver(path, extra_roots=extra_roots)
        except (log_inspect.LogInspectError, OSError, ValueError, TypeError) as exc:
            raise _reject("capture path rejected: %s" % exc) from None
        refuse_secret(Path(target))
        return Path(target)

    def contained_file(self, path: str, *, extra_roots: str = "") -> str:
        """Canonical path of a contained regular file (executables, PDBs, symbols)."""
        return str(self._resolve(path, extra_roots))

    def open_reader(self, path: str, *, extra_roots: str = "", max_bytes: int | None = None):
        target = self._resolve(path, extra_roots)
        stack = contextlib.ExitStack()
        try:
            handle, opened = stack.enter_context(self._opener(target, extra_roots))
        except (log_inspect.LogInspectError, OSError, ValueError) as exc:
            stack.close()
            raise _reject("capture could not be opened safely: %s" % type(exc).__name__) from None
        changed = {"value": False}

        def closer() -> None:
            try:
                stack.close()
            except PermissionError:
                changed["value"] = True
                raise debug_error(INPUT_CHANGED, "the capture changed while it was being read") from None

        try:
            size = int(opened.st_size)
            head = handle.read(min(size, SNIFF_HEAD_BYTES))
            reader = FileByteReader(handle, size, closer=closer)
            kind = sniff_kind(head, target.name, reader)
            cap = min(size_cap(kind), int(max_bytes) if max_bytes else size_cap(kind))
            if size > cap:
                raise debug_error(CAPTURE_TOO_LARGE, "%s captures are limited to %d bytes"
                                  % (kind, cap))
            key = (int(opened.st_dev), int(opened.st_ino), size, int(opened.st_mtime_ns))
            sha = self._cache.get(key)
            if sha is None:
                sha = stream_sha256(handle, size, clock=self._clock)
                self._cache.put(key, sha)
            identity = CaptureIdentity(
                path=str(target), label=_display_label(target), size=size,
                dev=key[0], ino=key[1], mtime_ns=key[3], sha256=sha, kind=kind)
        except BaseException:
            try:
                stack.close()
            except Exception:
                pass
            raise
        return reader, identity

    def sniff(self, reader, name: str) -> str:
        head = reader.read(0, min(reader.size, SNIFF_HEAD_BYTES))
        return sniff_kind(head, name, reader)

    def is_dir(self, path: str, *, extra_roots: str = "") -> bool:
        try:
            target = self._contained_dir(path, extra_roots)
        except Exception:
            return False
        return target is not None

    def _contained_dir(self, path: str, extra_roots: str) -> Path | None:
        text = str(path or "").strip()
        if not text or "\x00" in text or file_ops._foreign_absolute(text):
            return None
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = file_ops.workspace_root() / candidate
        try:
            file_ops._require_no_reparse_components(Path(os.path.normpath(str(candidate))))
            resolved = file_ops.resolve_repository_read_path(
                str(candidate), allow_workspace_root=False, reject_sensitive=True,
                extra_roots=extra_roots)
            info = resolved.lstat()
        except (OSError, PermissionError, ValueError, TypeError):
            return None
        if not stat.S_ISDIR(info.st_mode) or file_ops._is_reparse_point(resolved):
            return None
        return resolved

    def list_dir(self, path: str, *, extra_roots: str = "",
                 max_files: int = MAX_DIRECTORY_FILES) -> tuple[CaptureIdentity, ...]:
        """Regular files directly in a contained directory (non-recursive, sorted, capped)."""
        directory = self._contained_dir(path, extra_roots)
        if directory is None:
            raise _reject("directory is outside the allowed roots or not a plain directory")
        limit = max(1, min(MAX_DIRECTORY_FILES, int(max_files)))
        found: list[tuple[str, os.stat_result]] = []
        with os.scandir(directory) as iterator:
            for index, entry in enumerate(iterator):
                if index >= 4096:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or _secret_name(entry.name):
                    continue
                found.append((entry.name, info))
        found.sort(key=lambda item: item[0])
        out = []
        for name, info in found[:limit]:
            full = directory / name
            out.append(CaptureIdentity(
                path=str(full), label=_display_label(full), size=int(info.st_size),
                dev=int(info.st_dev), ino=int(info.st_ino), mtime_ns=int(info.st_mtime_ns),
                sha256="", kind=""))
        return tuple(out)

    # -- run-time identity and staging ------------------------------------

    @staticmethod
    def current(identity: CaptureIdentity) -> CaptureIdentity | None:
        """The file's identity now (no-follow), or None when it is gone."""
        try:
            info = os.lstat(identity.path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return CaptureIdentity(identity.path, identity.label, int(info.st_size), int(info.st_dev),
                               int(info.st_ino), int(info.st_mtime_ns), identity.sha256,
                               identity.kind)

    @staticmethod
    def staging_for(identity: CaptureIdentity, state_dir: str) -> str:
        if identity.size <= COPY_STAGING_MAX_BYTES:
            return "copy"
        if os.name != "nt":
            try:
                if os.stat(state_dir).st_dev == identity.dev:
                    return "hardlink"
            except OSError:
                pass
        return "path"

    def stage(self, identity: CaptureIdentity, rundir: str, *, strategy: str,
              dest_name: str = "", extra_roots: str = "") -> str:
        """Place the capture for the debugger; returns the path bound to ``{input}``."""
        # The staged name is bound into debugger argv: keep only a plain suffix.
        suffix = Path(identity.path).suffix[:16]
        if not _PLAIN_SUFFIX.fullmatch(suffix):
            suffix = ".bin"
        name = dest_name or ("capture" + suffix)
        dest = Path(rundir) / "in" / name
        if strategy == "copy":
            copy_guarded(self._opener, identity, dest, extra_roots)
            return str(dest)
        if strategy == "hardlink":
            os.link(identity.path, dest, follow_symlinks=False)
            info = os.lstat(dest)
            if (info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns) != (
                    identity.ino, identity.dev, identity.size, identity.mtime_ns):
                os.unlink(dest)
                raise debug_error(INPUT_CHANGED, "the capture changed before it was staged")
            return str(dest)
        current = self.current(identity)
        if current is None or not current.same_file(identity):
            raise debug_error(INPUT_CHANGED, "the capture changed before the run started")
        return identity.path

    def stage_file(self, source: str, dest: str, *, extra_roots: str = "") -> None:
        """Copy a contained symbol file (PE, PDB, ELF) to ``dest`` (0600, exclusive)."""
        copy_file_guarded(self._opener, source, Path(dest), extra_roots)


def copy_guarded(opener, identity: CaptureIdentity, dest: Path, extra_roots: str = "") -> None:
    """Copy a capture into a new 0600 file through the guarded no-follow open."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) \
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        with opener(Path(identity.path), extra_roots) as (handle, opened):
            if (int(opened.st_dev), int(opened.st_ino), int(opened.st_size),
                    int(opened.st_mtime_ns)) != (identity.dev, identity.ino, identity.size,
                                                 identity.mtime_ns):
                raise debug_error(INPUT_CHANGED, "the capture changed before it was staged")
            fd = os.open(dest, flags, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as out:
                    handle.seek(0)
                    copied = 0
                    while copied < identity.size:
                        chunk = handle.read(min(_CHUNK, identity.size - copied))
                        if not chunk:
                            break
                        out.write(chunk)
                        copied += len(chunk)
            finally:
                os.close(fd)
            if copied != identity.size:
                raise debug_error(INPUT_CHANGED, "the capture shrank while it was staged")
    except PermissionError:
        raise debug_error(INPUT_CHANGED, "the capture changed while it was staged") from None
    except (log_inspect.LogInspectError, OSError) as exc:
        if isinstance(exc, FileExistsError):
            raise
        raise _reject("capture could not be staged: %s" % type(exc).__name__) from None


def copy_file_guarded(opener, source: str, dest: Path, extra_roots: str = "",
                      max_bytes: int = 4 * GIB) -> None:
    """Copy a contained file (a PE, PDB or ELF for the symbolizer) into the run dir."""
    with opener(Path(source), extra_roots) as (handle, opened):
        size = int(opened.st_size)
        if size > max_bytes:
            raise debug_error(CAPTURE_TOO_LARGE, "symbol file is too large to stage")
        identity = CaptureIdentity(source, Path(source).name, size, int(opened.st_dev),
                                   int(opened.st_ino), int(opened.st_mtime_ns), "", "")
    copy_guarded(opener, identity, dest, extra_roots)


@contextlib.contextmanager
def share_deny_handle(path: str) -> Iterator[None]:
    """Windows: hold a handle that shares read only for the run (no-op elsewhere)."""
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING, OPEN_REPARSE_POINT.
    handle = create(str(path), 0x80000000, 0x00000001, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise debug_error(INPUT_CHANGED, "the capture is open for writing elsewhere")
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


__all__ = [
    "COPY_STAGING_MAX_BYTES", "FileByteReader", "GuardedCaptureSource", "IdentityCache",
    "copy_file_guarded", "copy_guarded", "refuse_secret", "share_deny_handle", "size_cap",
    "sniff_kind", "stream_sha256",
]
