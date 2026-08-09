"""Bounded, deterministic inspection of one guarded text log file."""
from __future__ import annotations

from collections import Counter, deque
import contextlib
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import time

import file_ops


DEFAULT_MAX_FILE_BYTES = 64_000_000
HARD_MAX_FILE_BYTES = 256_000_000
DEFAULT_MAX_SCAN_BYTES = 4_000_000
HARD_MAX_SCAN_BYTES = 16_000_000
DEFAULT_MAX_LINES = 10_000
HARD_MAX_LINES = 50_000
DEFAULT_MAX_LINE_BYTES = 4_096
HARD_MAX_LINE_BYTES = 16_384
DEFAULT_MAX_RESULTS = 100
HARD_MAX_RESULTS = 500
DEFAULT_MAX_OUTPUT_BYTES = 256_000
HARD_MAX_OUTPUT_BYTES = 512_000
DEFAULT_TIMEOUT_SECONDS = 5.0
HARD_MAX_TIMEOUT_SECONDS = 15.0
DEFAULT_CONTEXT_LINES = 2
HARD_MAX_CONTEXT_LINES = 10
HARD_MAX_TAIL_LINES = 50_000
MESSAGE_CHARS = 500
CONTEXT_CHARS = 1_000

LEVELS = (
    "TRACE", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR",
    "CRITICAL", "FATAL", "PANIC", "EXCEPTION",
)
LEVEL_ALIASES = {"WARN": "WARNING", "ERR": "ERROR", "SEVERE": "ERROR"}
FAILURE_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL", "PANIC", "EXCEPTION"})

_TIMESTAMP_RE = re.compile(
    r"^\s*\[?(?P<timestamp>"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
    r")\]?\s*"
)
_LEVEL_RE = re.compile(
    r"(?i)(?<![A-Za-z])"
    r"(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|SEVERE|CRITICAL|FATAL|PANIC|EXCEPTION)"
    r"(?![A-Za-z])"
)
_BRACKET_SOURCE_RE = re.compile(r"^\s*\[([^\]\r\n]{1,120})\]\s*")
_SOURCE_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-/]{0,119}$")
_SOURCE_PREFIX_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-/]{0,119}):\s*")
_FAILURE_HINT_RE = re.compile(
    r"(?i)(?:^\s*(?:traceback|fatal|panic)\b|\bunhandled exception\b|\bfailed\b)"
)
_WARNING_HINT_RE = re.compile(r"(?i)\bwarn(?:ing)?\b")
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_HEX_RE = re.compile(r"(?i)\b0x[0-9a-f]+\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?![A-Za-z])")
_SPACE_RE = re.compile(r"\s+")


class LogInspectError(RuntimeError):
    """A stable rejection from the guarded log inspection surface."""


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
    return max(0.05, min(value, HARD_MAX_TIMEOUT_SECONDS))


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise LogInspectError("log inspection exceeded the timeout ceiling")


def _requested_path(raw):
    text = str(raw or "").strip()
    if not text or "\x00" in text or file_ops._foreign_absolute(text):
        raise LogInspectError("log path must be a non-empty native path")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_reparse_components(path):
    current = Path(os.path.normpath(str(path)))
    while True:
        if file_ops._is_reparse_point(current):
            raise LogInspectError("log path must not traverse a symlink or junction")
        parent = current.parent
        if parent == current:
            return
        current = parent


def resolve_log_path(path, *, extra_roots=""):
    requested = _requested_path(path)
    _reject_reparse_components(requested)
    try:
        target = file_ops.resolve_repository_read_path(
            str(path), allow_workspace_root=False, reject_sensitive=True,
            extra_roots=extra_roots,
        )
        metadata = target.lstat()
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise LogInspectError("log path rejected: %s" % exc) from exc
    if file_ops._is_reparse_point(target):
        raise LogInspectError("log path must not be a symlink or junction")
    if not stat.S_ISREG(metadata.st_mode):
        raise LogInspectError("log path must be a regular file")
    return target


def _opened_handle_path(fd):
    if os.name == "nt":
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ]
        function.restype = ctypes.c_uint32
        needed = function(handle, None, 0, 0)
        if not needed:
            raise OSError(ctypes.get_last_error(), "could not resolve opened log handle")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not function(handle, buffer, len(buffer), 0):
            raise OSError(ctypes.get_last_error(), "could not resolve opened log handle")
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
            raise PermissionError("opened log was deleted during validation")
        return Path(value)
    raise PermissionError("platform cannot validate an opened log handle")


@contextlib.contextmanager
def _open_guarded_binary(path, extra_roots):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if os.name == "nt":
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
            raise OSError(ctypes.get_last_error(), "could not safely open log")
        info = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        get_info.restype = ctypes.c_int
        if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(raw_handle)
            raise OSError(ctypes.get_last_error(), "could not inspect opened log")
        if info.attributes & 0x00000400:
            kernel32.CloseHandle(raw_handle)
            raise PermissionError("replacement symlink or junction is not inspected")
        try:
            fd = msvcrt.open_osfhandle(raw_handle, flags)
        except Exception:
            kernel32.CloseHandle(raw_handle)
            raise
    else:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not opened.st_ino:
            raise PermissionError("opened log is not an identifiable regular file")
        actual = file_ops.resolve_repository_read_path(
            str(_opened_handle_path(fd)), allow_workspace_root=False,
            reject_sensitive=True, extra_roots=extra_roots,
        )
        current = actual.stat(follow_symlinks=False)
        if not os.path.samestat(opened, current):
            raise PermissionError("log changed while validating its opened handle")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield handle, opened
        after = os.fstat(fd)
        if file_ops._is_reparse_point(actual):
            raise PermissionError("log became a symlink or junction during inspection")
        current_after = actual.lstat()
        if (
            not os.path.samestat(opened, after)
            or not os.path.samestat(after, current_after)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise PermissionError("log changed while it was being inspected")
    finally:
        os.close(fd)


def _read_window(handle, size, max_scan_bytes, tail_lines, max_lines, deadline):
    _check_deadline(deadline)
    tail_mode = tail_lines > 0
    start = max(0, size - max_scan_bytes) if tail_mode else 0
    handle.seek(start)
    raw = handle.read(min(max_scan_bytes, size - start))
    _check_deadline(deadline)
    if b"\x00" in raw:
        raise LogInspectError("log window contains NUL bytes and is not text")
    if tail_mode and start:
        newline = raw.find(b"\n")
        raw = b"" if newline < 0 else raw[newline + 1:]
    elif not tail_mode and start + len(raw) < size:
        newline = raw.rfind(b"\n")
        raw = b"" if newline < 0 else raw[:newline + 1]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LogInspectError("log window is not valid UTF-8 text") from exc
    lines = deque(maxlen=tail_lines) if tail_mode else []
    lines_seen = 0
    line_cap_truncated = False
    with io.StringIO(text) as stream:
        for raw_line in stream:
            lines_seen += 1
            if lines_seen % 1024 == 0:
                _check_deadline(deadline)
            line = raw_line.rstrip("\r\n")
            if tail_mode:
                lines.append(line)
            elif len(lines) < max_lines:
                lines.append(line)
            else:
                line_cap_truncated = True
                break
    lines = list(lines)
    if tail_mode and len(lines) > max_lines:
        lines = lines[-max_lines:]
        line_cap_truncated = True
    return lines, {
        "source_bytes": size,
        "window_start_byte": start,
        "bytes_read": len(raw),
        "tail": tail_mode,
        "tail_lines": tail_lines,
        "byte_truncated": bool(start or start + len(raw) < size),
        "line_cap_truncated": line_cap_truncated,
        "lines_seen": lines_seen,
        "line_numbers": "window-relative" if start else "file-relative",
    }


def _clean(value, limit=MESSAGE_CHARS):
    text = str(value if value is not None else "")
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    text = "".join(character if character.isprintable() else "?" for character in text)
    return text[:limit]


def _json_field(payload, names):
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return ""


def _normalize_level(value):
    level = str(value or "").strip().upper()
    level = LEVEL_ALIASES.get(level, level)
    return level if level in LEVELS else ""


def _parse_line(line):
    stripped = line.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict):
            level = _normalize_level(_json_field(
                payload, ("level", "severity", "loglevel", "log_level"),
            ))
            timestamp = _clean(_json_field(
                payload, ("timestamp", "time", "@timestamp", "datetime"),
            ), 100)
            source = _clean(_json_field(
                payload, ("source", "logger", "module", "component", "service"),
            ), 120)
            message_value = _json_field(payload, ("message", "msg", "event", "error"))
            if isinstance(message_value, (dict, list)):
                message_value = json.dumps(
                    message_value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
            message = _clean(message_value or stripped)
            return timestamp, level, source, message

    timestamp = ""
    remainder = stripped
    match = _TIMESTAMP_RE.match(remainder)
    if match:
        timestamp = match.group("timestamp")
        remainder = remainder[match.end():]
    level_match = _LEVEL_RE.search(remainder[:240])
    level = ""
    source = ""
    if level_match:
        level = _normalize_level(level_match.group(1))
        before = remainder[:level_match.start()].strip(" []:-")
        after = remainder[level_match.end():].lstrip()
        if after.startswith("]"):
            after = after[1:].lstrip(" :-")
        else:
            after = after.lstrip(" :-")
        if before and _SOURCE_TOKEN_RE.fullmatch(before):
            source = before
        else:
            source_match = _BRACKET_SOURCE_RE.match(after)
            if source_match:
                candidate = source_match.group(1).strip()
                if not _normalize_level(candidate):
                    source = candidate
                    after = after[source_match.end():]
            else:
                source_match = _SOURCE_PREFIX_RE.match(after)
                if source_match:
                    source = source_match.group(1)
                    after = after[source_match.end():]
        remainder = after
    elif _FAILURE_HINT_RE.search(remainder):
        level = "ERROR"
    elif _WARNING_HINT_RE.search(remainder):
        level = "WARNING"
    return _clean(timestamp, 100), level, _clean(source, 120), _clean(remainder or stripped)


def _template(message):
    value = _UUID_RE.sub("<uuid>", message)
    value = _HEX_RE.sub("<hex>", value)
    value = _NUMBER_RE.sub("<n>", value)
    return _SPACE_RE.sub(" ", value).strip().casefold()[:MESSAGE_CHARS]


def _encoded(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_result(report):
    return _encoded(report)


def _fit_output(report, max_output):
    report["output_bytes"] = 0
    while True:
        for _ in range(10):
            actual = len(_encoded(report).encode("utf-8"))
            if actual == report["output_bytes"]:
                break
            report["output_bytes"] = actual
        actual = len(_encoded(report).encode("utf-8"))
        if actual <= max_output and actual == report["output_bytes"]:
            return report
        removed = False
        for name in ("repeated_messages", "clusters", "sources"):
            if report[name]:
                report[name].pop()
                report["details_truncated"] = True
                removed = True
                break
        if not removed:
            for name in ("last_failure", "first_failure"):
                context = report.get(name, {}).get("context", [])
                if context:
                    context.pop()
                    report["details_truncated"] = True
                    removed = True
                    break
        if not removed:
            raise LogInspectError("log summary exceeds the output byte ceiling")


def inspect_log(
    path, *, tail_lines=0, context_lines=DEFAULT_CONTEXT_LINES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    max_scan_bytes=DEFAULT_MAX_SCAN_BYTES, max_lines=DEFAULT_MAX_LINES,
    max_line_bytes=DEFAULT_MAX_LINE_BYTES, max_results=DEFAULT_MAX_RESULTS,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES, timeout=DEFAULT_TIMEOUT_SECONDS,
    extra_roots="",
):
    """Inspect one text log through a bounded, already-validated file handle."""
    limits = {
        "max_file_bytes": _bounded_int(
            max_file_bytes, DEFAULT_MAX_FILE_BYTES, 1, HARD_MAX_FILE_BYTES,
        ),
        "max_scan_bytes": _bounded_int(
            max_scan_bytes, DEFAULT_MAX_SCAN_BYTES, 1, HARD_MAX_SCAN_BYTES,
        ),
        "max_lines": _bounded_int(max_lines, DEFAULT_MAX_LINES, 1, HARD_MAX_LINES),
        "max_line_bytes": _bounded_int(
            max_line_bytes, DEFAULT_MAX_LINE_BYTES, 64, HARD_MAX_LINE_BYTES,
        ),
        "max_results": _bounded_int(
            max_results, DEFAULT_MAX_RESULTS, 1, HARD_MAX_RESULTS,
        ),
        "max_output_bytes": _bounded_int(
            max_output_bytes, DEFAULT_MAX_OUTPUT_BYTES, 1_024, HARD_MAX_OUTPUT_BYTES,
        ),
        "timeout_seconds": _bounded_timeout(timeout),
        "context_lines": _bounded_int(
            context_lines, DEFAULT_CONTEXT_LINES, 0, HARD_MAX_CONTEXT_LINES,
        ),
        "tail_lines": _bounded_int(tail_lines, 0, 0, HARD_MAX_TAIL_LINES),
    }
    deadline = time.monotonic() + limits["timeout_seconds"]
    target = resolve_log_path(path, extra_roots=extra_roots)
    try:
        with _open_guarded_binary(target, extra_roots) as (handle, metadata):
            if metadata.st_size > limits["max_file_bytes"]:
                raise LogInspectError("log exceeds the file byte ceiling")
            lines, window = _read_window(
                handle, metadata.st_size, limits["max_scan_bytes"],
                limits["tail_lines"], limits["max_lines"], deadline,
            )
    except LogInspectError:
        raise
    except (OSError, PermissionError, ValueError) as exc:
        raise LogInspectError("log could not be safely read: %s" % exc) from exc

    available_lines = window["lines_seen"]
    selected = lines
    selected_offset = max(0, available_lines - len(selected)) if limits["tail_lines"] else 0
    line_truncated = 0
    records = []
    level_counts = Counter()
    source_counts = Counter()
    repeat_counts = Counter()
    repeat_samples = {}
    clusters = {}
    failure_indexes = []
    for index, raw_line in enumerate(selected, 1):
        _check_deadline(deadline)
        encoded_line = raw_line.encode("utf-8")
        if len(encoded_line) > limits["max_line_bytes"]:
            encoded_line = encoded_line[:limits["max_line_bytes"]]
            raw_line = encoded_line.decode("utf-8", errors="ignore")
            line_truncated += 1
        line_number = selected_offset + index
        timestamp, level, source, message = _parse_line(raw_line)
        _check_deadline(deadline)
        template = _template(message)
        record = {
            "line": line_number, "timestamp": timestamp, "level": level,
            "source": source, "message": message,
            "raw": _clean(raw_line, CONTEXT_CHARS),
        }
        records.append(record)
        level_counts[level or "UNCLASSIFIED"] += 1
        if source:
            source_counts[source] += 1
        if template:
            repeat_counts[template] += 1
            repeat_samples.setdefault(template, message)
        if level in FAILURE_LEVELS or level == "WARNING":
            group = "error" if level in FAILURE_LEVELS else "warning"
            key = (group, template or message.casefold())
            row = clusters.setdefault(key, {
                "kind": group, "template": template or message.casefold(),
                "sample": message, "count": 0, "first_line": line_number,
                "last_line": line_number, "first_timestamp": timestamp,
                "last_timestamp": timestamp, "sources": set(),
            })
            row["count"] += 1
            row["last_line"] = line_number
            if timestamp:
                if not row["first_timestamp"]:
                    row["first_timestamp"] = timestamp
                row["last_timestamp"] = timestamp
            if source:
                row["sources"].add(source)
        if level in FAILURE_LEVELS:
            failure_indexes.append(len(records) - 1)

    cluster_rows = []
    for row in clusters.values():
        row = dict(row)
        row["sources"] = sorted(row["sources"], key=lambda value: (value.casefold(), value))
        cluster_rows.append(row)
    cluster_rows.sort(key=lambda row: (-row["count"], row["first_line"], row["template"]))
    repeat_rows = [
        {"template": template, "sample": repeat_samples[template], "count": count}
        for template, count in repeat_counts.items() if count > 1
    ]
    repeat_rows.sort(key=lambda row: (-row["count"], row["template"]))
    source_rows = [
        {"source": source, "count": count}
        for source, count in source_counts.items()
    ]
    source_rows.sort(key=lambda row: (-row["count"], row["source"].casefold(), row["source"]))

    remaining = limits["max_results"]
    selected_clusters = cluster_rows[:remaining]
    remaining -= len(selected_clusters)
    selected_repeats = repeat_rows[:remaining]
    remaining -= len(selected_repeats)
    selected_sources = source_rows[:remaining]

    def failure_detail(record_index):
        if record_index is None:
            return None
        record = records[record_index]
        radius = limits["context_lines"]
        context = [
            {"line": rows["line"], "text": rows["raw"]}
            for rows in records[
                max(0, record_index - radius):record_index + radius + 1
            ]
        ]
        return {
            key: record[key]
            for key in ("line", "timestamp", "level", "source", "message")
        } | {"context": context}

    failures = len(failure_indexes)
    warnings = level_counts["WARNING"]
    timestamps = [record["timestamp"] for record in records if record["timestamp"]]
    report = {
        "ok": True,
        "path": str(target),
        "sha256_window": hashlib.sha256("\n".join(selected).encode("utf-8")).hexdigest(),
        "window": window,
        "summary": {
            "lines_available": available_lines,
            "lines_inspected": len(selected),
            "error_lines": failures,
            "warning_lines": warnings,
            "unique_error_warning_clusters": len(cluster_rows),
            "repeated_message_groups": len(repeat_rows),
            "unique_sources": len(source_rows),
            "line_truncated": line_truncated,
        },
        "levels": dict(sorted(level_counts.items())),
        "timestamps": {
            "count": len(timestamps),
            "first": timestamps[0] if timestamps else "",
            "last": timestamps[-1] if timestamps else "",
        },
        "clusters": selected_clusters,
        "repeated_messages": selected_repeats,
        "sources": selected_sources,
        "first_failure": failure_detail(failure_indexes[0] if failures else None),
        "last_failure": failure_detail(failure_indexes[-1] if failures else None),
        "details_truncated": (
            len(cluster_rows) + len(repeat_rows) + len(source_rows)
            > limits["max_results"]
        ),
        "scan_truncated": bool(
            window["byte_truncated"] or window["line_cap_truncated"]
        ),
        "limits": limits,
        "output_bytes": 0,
    }
    return _fit_output(report, limits["max_output_bytes"])
