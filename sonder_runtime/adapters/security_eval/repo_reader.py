"""Read-only, confined access to one repo root: source windows and retrieval context.

Confinement rules (every one is enforced here, none is delegated to the caller):
- the root is absolute, an existing plain directory (not a symlink, junction or alias of one), and
  inside the operator's allowlist; an empty allowlist refuses every root;
- a record's ``file`` field may not hold ``..``, an absolute path, a drive letter or an alternate
  data stream;
- the walk never follows a symlink or junction and never lists one; ``.git`` and credential
  directories, secret files, ``.env*``, binary files and files over the byte cap are skipped;
- every open is a no-follow open (``FILE_FLAG_OPEN_REPARSE_POINT`` on Windows, ``O_NOFOLLOW`` on
  POSIX) whose handle path is checked to lie inside the root, so a directory swapped for a link after
  indexing is refused at open time instead of read.
Truncation (file count, bytes, time) is reported, never silent. Nothing is written or executed.
Not defended: a hard link inside the root to a file elsewhere reads as an ordinary file.
"""
from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from typing import Callable

import sonder_runtime.adapters.filesystem.file_ops as file_ops

from ...application.security_eval.ports import SourceWindow
from ...domain.security_eval.context_select import (
    ContextBundle, ContextHit, MAX_SNIPPET_LINE_CHARS, classify_hit, enclosing_declaration,
    extract_identifiers, is_guard_line, pack_context)
from ...domain.security_eval.errors import (
    DatasetRecordError, PathEscapeRefused, RepoRootRefused, SourceRefused, SourceUnavailable)
from ...domain.security_eval.records import LineRange, check_relative_path

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SKIP_DIRS = frozenset(
    name.casefold() for name in (
        set(file_ops.SENSITIVE_READ_DIRECTORIES) | set(file_ops.CREDENTIAL_READ_DIRECTORIES)
        | {"node_modules", "__pycache__"}))
_SECRET_NAMES = frozenset(name.casefold() for name in (
    set(file_ops.SECRET_FILES) | set(file_ops.CREDENTIAL_READ_FILES)))
_SECRET_SUFFIXES = frozenset(suffix.casefold() for suffix in file_ops.SECRET_SUFFIXES)
_BINARY_PROBE = 4096
_MAX_DEPTH = 40
_GUARD_WINDOW_BEFORE_CALLER = 5
_GUARD_WINDOW_NO_FUNCTION = 40


@dataclass(frozen=True)
class RepoLimits:
    max_files: int = 20000
    max_bytes: int = 64 << 20
    max_file_bytes: int = 1 << 20
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class RetrievalSettings:
    max_identifiers: int = 12
    max_hits_per_kind: int = 3
    snippet_lines: int = 3
    common_hit_limit: int = 200
    max_context_chars: int = 6000


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(child: str, parent: str) -> bool:
    low, high = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([low, high]) == high
    except ValueError:
        return False


def _is_reparse(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _sensitive_name(name: str) -> bool:
    folded = name.casefold()
    return (folded in _SECRET_NAMES or folded.startswith(".env")
            or os.path.splitext(folded)[1] in _SECRET_SUFFIXES)


def _opened_handle_path(fd: int) -> str | None:
    """The path of an open descriptor, or ``None`` where the platform cannot say."""
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
            raise OSError(ctypes.get_last_error(), "could not resolve the opened file handle")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not function(handle, buffer, len(buffer), 0):
            raise OSError(ctypes.get_last_error(), "could not resolve the opened file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            return "\\\\" + value[8:]
        return value[4:] if value.startswith("\\\\?\\") else value
    for link in ("/proc/self/fd/%d" % fd, "/dev/fd/%d" % fd):
        try:
            value = os.readlink(link)
        except OSError:
            continue
        if value.endswith(" (deleted)"):
            raise PathEscapeRefused("the opened file was deleted while it was validated")
        return value
    return None


def _open_no_follow(path: str) -> int:
    """Open ``path`` read-only without following a link in its last component."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    import ctypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    # GENERIC_READ, share read|write|delete, OPEN_EXISTING, SEQUENTIAL_SCAN | OPEN_REPARSE_POINT.
    raw = create(path, 0x80000000, 0x7, None, 3, 0x08000000 | 0x00200000, None)
    if raw == ctypes.c_void_p(-1).value or raw is None:
        raise OSError(ctypes.get_last_error(), "could not open the source file")
    info = FileAttributeTagInfo()
    inspect = kernel32.GetFileInformationByHandleEx
    inspect.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    inspect.restype = ctypes.c_int
    if not inspect(raw, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(raw)
        raise OSError(ctypes.get_last_error(), "could not inspect the source file")
    if info.attributes & 0x400:
        kernel32.CloseHandle(raw)
        raise PathEscapeRefused("the source file is a symlink or junction")
    try:
        return msvcrt.open_osfhandle(raw, flags)
    except Exception:
        kernel32.CloseHandle(raw)
        raise


class ConfinedRepo:
    """One repo root, read-only. Single-threaded; the index is built once, lazily."""

    def __init__(self, root: str, allowlist, limits: RepoLimits | None = None,
                 settings: RetrievalSettings | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = limits or RepoLimits()
        self._settings = settings or RetrievalSettings()
        self._clock = clock
        self._root = self._validate_root(root, allowlist)
        self._index: dict[str, list[str]] | None = None
        self._paths: tuple[str, ...] = ()
        self._truncated: list[str] = []
        self._skipped = {"links": 0, "sensitive": 0, "binary": 0, "large": 0, "unreadable": 0}
        self._bytes = 0

    @staticmethod
    def _validate_root(root, allowlist) -> str:
        if not isinstance(root, str) or not root.strip() or "\x00" in root:
            raise RepoRootRefused("repo_root is empty")
        if not os.path.isabs(root):
            raise RepoRootRefused("repo_root must be an absolute path")
        absolute = os.path.abspath(root)
        entries = [entry for entry in (allowlist or ()) if isinstance(entry, str)
                   and entry.strip() and os.path.isabs(entry)]
        if not entries:
            raise RepoRootRefused("no repo root is allowed: SONDER_SECURITY_EVAL_REPO_ROOTS is empty")
        if not os.path.isdir(absolute):
            raise RepoRootRefused("repo_root is not an existing directory")
        if _is_reparse(absolute):
            raise RepoRootRefused("repo_root may not be a symlink or junction")
        if _norm(os.path.realpath(absolute)) != _norm(absolute):
            raise RepoRootRefused("repo_root traverses a symlink, junction or short-name alias")
        if not any(_inside(absolute, entry) for entry in entries):
            raise RepoRootRefused("repo_root is outside SONDER_SECURITY_EVAL_REPO_ROOTS")
        return absolute

    @property
    def root(self) -> str:
        return self._root

    # -- confined reads -----------------------------------------------------------------
    @staticmethod
    def _check_rel(rel: str) -> str:
        try:
            return check_relative_path(rel)
        except DatasetRecordError as exc:
            raise PathEscapeRefused("file %r is not a plain relative path: %s" % (str(rel)[:80], exc)) from None

    @staticmethod
    def _refuse_sensitive(rel: str) -> None:
        parts = rel.split("/")
        if any(part.casefold() in _SKIP_DIRS for part in parts[:-1]) or _sensitive_name(parts[-1]):
            raise SourceRefused("file %r is a sensitive or secret path and is never read" % rel)

    def _read_bytes(self, rel: str) -> bytes:
        """Read one file by relative path through the no-follow open and the handle-path check."""
        current = self._root
        parts = rel.split("/")
        for position, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except OSError:
                raise SourceUnavailable("file %r does not exist" % rel) from None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                raise PathEscapeRefused("%r passes through a symlink or junction" % rel)
            if position < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise SourceUnavailable("file %r does not exist" % rel)
        try:
            descriptor = _open_no_follow(current)
        except PathEscapeRefused:
            raise
        except OSError:
            raise SourceUnavailable("file %r could not be opened" % rel) from None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise SourceUnavailable("%r is not a regular file" % rel)
            actual = _opened_handle_path(descriptor)
            if actual is not None and not _inside(actual, self._root):
                raise PathEscapeRefused("%r resolves outside the repo root" % rel)
            if actual is None or os.name != "nt":
                if not os.path.samestat(opened, os.stat(current, follow_symlinks=False)):
                    raise PathEscapeRefused("%r changed while it was validated" % rel)
            if opened.st_size > self._limits.max_file_bytes:
                raise SourceUnavailable("file %r is over the %d byte cap" % (rel, self._limits.max_file_bytes))
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read(self._limits.max_file_bytes + 1)
        finally:
            os.close(descriptor)

    def _read_lines(self, rel: str) -> list[str]:
        data = self._read_bytes(rel)
        if b"\x00" in data[:_BINARY_PROBE]:
            raise SourceRefused("file %r is binary" % rel)
        return data.decode("utf-8", errors="replace").splitlines()

    def read_window(self, rel: str, lines: LineRange, pad: int) -> SourceWindow:
        rel = self._check_rel(rel)
        self._refuse_sensitive(rel)
        content = self._read_lines(rel)
        total = len(content)
        if lines.start > total:
            raise SourceUnavailable("lines %s are past the end of %r (%d lines)" % (lines.as_text(), rel, total))
        start = max(1, lines.start - max(0, pad))
        end = min(total, lines.end + max(0, pad))
        return SourceWindow(rel, start, end, "\n".join(content[start - 1:end]), total)

    def read_chunks(self, rel: str, max_chars: int) -> tuple[SourceWindow, ...]:
        rel = self._check_rel(rel)
        self._refuse_sensitive(rel)
        content = self._read_lines(rel)
        total = len(content)
        chunks: list[SourceWindow] = []
        first = 1
        pending: list[str] = []
        size = 0
        for number, line in enumerate(content, start=1):
            line = line[:max_chars]
            cost = len(line) + (1 if pending else 0)
            if pending and size + cost > max_chars:
                chunks.append(SourceWindow(rel, first, number - 1, "\n".join(pending), total))
                first, pending, size, cost = number, [], 0, len(line)
            pending.append(line)
            size += cost
        if pending:
            chunks.append(SourceWindow(rel, first, total, "\n".join(pending), total))
        return tuple(chunks)

    # -- the index ----------------------------------------------------------------------
    def _walk(self, rel_dir: str, depth: int, started: float, found: list[str]) -> bool:
        """Collect files below ``rel_dir``; ``False`` when a cap stopped the walk."""
        absolute = os.path.join(self._root, rel_dir) if rel_dir else self._root
        try:
            entries = sorted(os.scandir(absolute), key=lambda entry: entry.name)
        except OSError:
            self._skipped["unreadable"] += 1
            return True
        for entry in entries:
            if self._clock() - started > self._limits.timeout_seconds:
                self._truncated.append("timeout")
                return False
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                self._skipped["unreadable"] += 1
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                self._skipped["links"] += 1
                continue
            rel = "%s/%s" % (rel_dir, entry.name) if rel_dir else entry.name
            if stat.S_ISDIR(info.st_mode):
                if entry.name.casefold() in _SKIP_DIRS or depth >= _MAX_DEPTH:
                    self._skipped["sensitive"] += 1
                    continue
                if not self._walk(rel, depth + 1, started, found):
                    return False
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if _sensitive_name(entry.name):
                self._skipped["sensitive"] += 1
                continue
            if info.st_size > self._limits.max_file_bytes:
                self._skipped["large"] += 1
                continue
            if len(found) >= self._limits.max_files:
                self._truncated.append("max_files")
                return False
            if self._bytes + info.st_size > self._limits.max_bytes:
                self._truncated.append("max_bytes")
                return False
            try:
                data = self._read_bytes(rel)
            except (PathEscapeRefused, SourceUnavailable):
                self._skipped["unreadable"] += 1
                continue
            if b"\x00" in data[:_BINARY_PROBE]:
                self._skipped["binary"] += 1
                continue
            self._bytes += len(data)
            self._index[rel] = data.decode("utf-8", errors="replace").splitlines()  # type: ignore[index]
            found.append(rel)
        return True

    def _ensure_index(self) -> dict[str, list[str]]:
        if self._index is None:
            self._index = {}
            found: list[str] = []
            self._walk("", 0, self._clock(), found)
            self._paths = tuple(sorted(found))
        return self._index

    def indexed_paths(self) -> tuple[str, ...]:
        self._ensure_index()
        return self._paths

    def index_report(self) -> dict:
        self._ensure_index()
        return {"files": len(self._paths), "bytes": self._bytes,
                "truncated": sorted(set(self._truncated)), "skipped": dict(self._skipped)}

    # -- retrieval ----------------------------------------------------------------------
    def _snippet(self, lines: list[str], number: int) -> str:
        last = min(len(lines), number + max(0, self._settings.snippet_lines))
        return "\n".join(line[:MAX_SNIPPET_LINE_CHARS] for line in lines[number - 1:last])

    def retrieve(self, rel: str, lines: LineRange) -> ContextBundle:
        rel = self._check_rel(rel)
        self._refuse_sensitive(rel)
        target = self._read_lines(rel)
        if lines.start > len(target):
            raise SourceUnavailable("lines %s are past the end of %r" % (lines.as_text(), rel))
        window_text = "\n".join(target[lines.start - 1:lines.end])
        names = list(extract_identifiers(window_text, self._settings.max_identifiers))
        enclosing = enclosing_declaration(target, lines.start)
        function_name = enclosing[0].rsplit("::", 1)[-1] if enclosing else ""
        if function_name and function_name not in names:
            names.append(function_name)
        index = self._ensure_index()
        skipped: dict[str, tuple[str, ...]] = {}
        if self._truncated:
            skipped["index_truncated"] = tuple(sorted(set(self._truncated)))
        if not names:
            return pack_context([], self._settings.max_context_chars, self._settings.max_hits_per_kind, skipped)
        pattern = re.compile(r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % "|".join(
            re.escape(name) for name in sorted(names, key=lambda item: (-len(item), item))))
        found: dict[str, list[tuple[str, int, str]]] = {name: [] for name in names}
        for path in self._paths:
            for number, line in enumerate(index[path], start=1):
                seen_on_line: set[str] = set()
                for match in pattern.finditer(line):
                    name = match.group(0)
                    if name not in seen_on_line:
                        seen_on_line.add(name)
                        found[name].append((path, number, line))
        too_common = sorted(name for name, hits in found.items() if len(hits) > self._settings.common_hit_limit)
        no_hits = []
        hits: list[ContextHit] = []
        guard_floor = enclosing[1] if enclosing else max(1, lines.start - _GUARD_WINDOW_NO_FUNCTION)
        for name in sorted(names):
            if name in too_common:
                continue
            outside = [item for item in found[name]
                       if not (item[0] == rel and lines.start <= item[1] <= lines.end)]
            if not outside:
                no_hits.append(name)
                continue
            for path, number, line in outside:
                kind = classify_hit(line, name)
                if kind == "guard" and not (path == rel and guard_floor <= number < lines.start):
                    continue
                if kind in ("definition", "caller", "guard"):
                    hits.append(ContextHit(kind, path, number, name, self._snippet(index[path], number)))
                if kind == "caller":
                    hits.extend(self._guards_before(index[path], path, number, pattern, names))
        if too_common:
            skipped["too_common"] = tuple(too_common)
        if no_hits:
            skipped["no_hits"] = tuple(no_hits)
        return pack_context(hits, self._settings.max_context_chars, self._settings.max_hits_per_kind, skipped)

    def _guards_before(self, lines: list[str], path: str, caller_line: int, pattern, names) -> list[ContextHit]:
        out = []
        for number in range(max(1, caller_line - _GUARD_WINDOW_BEFORE_CALLER), caller_line):
            text = lines[number - 1]
            if not is_guard_line(text):
                continue
            match = pattern.search(text)
            if match:
                out.append(ContextHit("guard", path, number, match.group(0), self._snippet(lines, number)))
        return out
