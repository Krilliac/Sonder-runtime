"""Bounded, non-executing inspection of active-content risk in PDF files."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import stat
import time
import zlib
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops


DEFAULT_MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_DECODED_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STREAMS = 256
MAX_STREAMS = 1024
DEFAULT_MAX_SECONDS = 5.0
MAX_SECONDS = 15.0

_FEATURES = (
    ("javascript", (b"/JavaScript", b"/JS"), "high"),
    ("open_action", (b"/OpenAction",), "high"),
    ("additional_actions", (b"/AA",), "high"),
    ("launch_action", (b"/Launch",), "high"),
    ("remote_goto", (b"/GoToR",), "high"),
    ("embedded_file", (b"/EmbeddedFile", b"/EmbeddedFiles"), "high"),
    ("file_specification", (b"/Filespec",), "medium"),
    ("rich_media", (b"/RichMedia", b"/RichMediaActivation"), "high"),
    ("xfa", (b"/XFA",), "high"),
    ("submit_or_import", (b"/SubmitForm", b"/ImportData"), "high"),
    ("external_uri", (b"/URI",), "medium"),
    ("acroform", (b"/AcroForm",), "low"),
)
_STREAM_KEYWORD = b"stream"
_ENDSTREAM_KEYWORD = b"endstream"
_PDF_WHITESPACE = frozenset(b"\x00\t\n\x0c\r ")
_INLINE_WHITESPACE = frozenset(b"\x00\t\x0c ")
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
# ``stream`` right after ``/`` is part of a name and after ``%`` part of a comment.
_KEYWORD_LEFT_BOUNDARY = _PDF_WHITESPACE | (_PDF_DELIMITERS - frozenset(b"/%"))
_KEYWORD_RIGHT_BOUNDARY = _PDF_WHITESPACE | _PDF_DELIMITERS
_DICTIONARY_WINDOW = 8192
_NAME_ESCAPE_RE = re.compile(br"#([0-9A-Fa-f]{2})")
_FILTER_RE = re.compile(br"/Filter[\x00\t\n\x0c\r ]*(\[[^\]]*\]|/[A-Za-z0-9#]+)", re.S)
_FILTER_KEY_RE = re.compile(br"/Filter(?![A-Za-z0-9#])")
_NAME_RE = re.compile(br"/[A-Za-z0-9#]+")


class PdfRiskError(ValueError):
    """Raised when a PDF cannot be inspected safely."""


def _clamp_int(value, default, low, high):
    if value is None:
        value = default
    if type(value) is not int:
        raise PdfRiskError("numeric limits must be exact JSON integers")
    return max(low, min(value, high))


def _clamp_seconds(value):
    if type(value) not in (int, float):
        raise PdfRiskError("max_seconds must be an exact JSON number")
    number = float(value)
    if not (number > 0.0) or not math.isfinite(number):
        raise PdfRiskError("max_seconds must be finite and positive")
    return min(number, MAX_SECONDS)


def _deadline_check(deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("PDF inspection exceeded its deadline")


def _opened_handle_path(fd):
    if os.name == "nt":
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        needed = function(handle, None, 0, 0)
        if not needed:
            raise OSError(ctypes.get_last_error(), "could not resolve opened PDF handle")
        buffer = ctypes.create_unicode_buffer(needed + 1)
        if not function(handle, buffer, len(buffer), 0):
            raise OSError(ctypes.get_last_error(), "could not resolve opened PDF handle")
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
            raise PermissionError("opened PDF was deleted during validation")
        return Path(value)
    raise PermissionError("platform cannot validate an opened PDF handle")


@contextlib.contextmanager
def _open_guarded(path, extra_roots):
    requested = file_ops.resolve_repository_read_path(
        path, allow_workspace_root=False, reject_sensitive=True, extra_roots=extra_roots,
    )
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
            str(requested), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x00200000 | 0x08000000, None,
        )
        if raw_handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "could not safely open PDF")
        info = FileAttributeTagInfo()
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        get_info.restype = ctypes.c_int
        if not get_info(raw_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(raw_handle)
            raise OSError(ctypes.get_last_error(), "could not inspect opened PDF")
        if info.attributes & 0x00000400:
            kernel32.CloseHandle(raw_handle)
            raise PermissionError("symlink or junction PDFs are not inspected")
        try:
            fd = msvcrt.open_osfhandle(raw_handle, flags)
        except Exception:
            kernel32.CloseHandle(raw_handle)
            raise
    else:
        fd = os.open(requested, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not opened.st_ino:
            raise PermissionError("opened PDF is not an identifiable regular file")
        actual = file_ops.resolve_repository_read_path(
            str(_opened_handle_path(fd)), allow_workspace_root=False,
            reject_sensitive=True, extra_roots=extra_roots,
        )
        if not os.path.samestat(opened, actual.stat(follow_symlinks=False)):
            raise PermissionError("PDF changed while validating its opened handle")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            yield actual, handle, opened
        after = os.fstat(fd)
        if file_ops._is_reparse_point(actual):
            raise PermissionError("PDF became a symlink or junction during inspection")
        if (
            not os.path.samestat(opened, after)
            or not os.path.samestat(after, actual.lstat())
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise PermissionError("PDF changed while it was being inspected")
    finally:
        os.close(fd)


def _normalize_names(data):
    return _NAME_ESCAPE_RE.sub(lambda m: bytes((int(m.group(1), 16),)), data)


def _feature_counts(data):
    normalized = _normalize_names(data)
    found = {}
    for name, tokens, severity in _FEATURES:
        count = sum(len(re.findall(re.escape(token) + br"(?![A-Za-z0-9])", normalized)) for token in tokens)
        if count:
            found[name] = {"count": count, "severity": severity}
    return found


def _merge_features(target, source, location):
    for name, item in source.items():
        current = target.setdefault(name, {"count": 0, "severity": item["severity"], "locations": []})
        current["count"] += item["count"]
        if location not in current["locations"] and len(current["locations"]) < 8:
            current["locations"].append(location)


def _filters(dictionary):
    match = _FILTER_RE.search(dictionary)
    if not match:
        return []
    return [name[1:].decode("ascii", errors="replace") for name in _NAME_RE.findall(match.group(1))]


def _inner_stream_keyword(region, start, end):
    """Return the index of a standalone ``stream`` keyword inside ``region[start:end]``, or -1."""
    position = start
    while True:
        index = region.find(_STREAM_KEYWORD, position, end)
        if index < 0:
            return -1
        after = index + len(_STREAM_KEYWORD)
        if (
            index > 0
            and region[index - 1] in _KEYWORD_LEFT_BOUNDARY
            and (after >= len(region) or region[after] in _KEYWORD_RIGHT_BOUNDARY)
        ):
            return index
        position = after


def _skip_whitespace_back(region, index, floor):
    while index > floor and region[index - 1] in _PDF_WHITESPACE:
        index -= 1
    return index


def _stream_spans(region, deadline):
    """Yield ``(keyword, data_start, data_end)`` for each stream, scanning linearly.

    A ``stream`` keyword counts when it is a standalone token (whitespace or a
    delimiter on both sides) or when an EOL or ``>>`` (optionally followed by
    whitespace) precedes it.  A standalone keyword counts even when no ``>>``
    is found before it (a comment, or more whitespace than the dictionary
    window, between dictionary and keyword); ``_stream_dictionary`` then
    returns None, so the stream is reported as ambiguous instead of being
    silently dropped.  Non-EOL whitespace after the keyword is skipped
    up to the EOL that starts the data, as readers do.  A counted keyword with
    no EOL after it yields ``data_start is None`` so the caller can mark the
    scan incomplete instead of silently dropping the stream.  Each
    ``endstream`` search starts after the previous stream ends, so the whole
    region is scanned at most a constant number of times.  An unterminated
    stream yields ``data_end == -1`` and ends the scan of the region.
    """
    position = 0
    while True:
        _deadline_check(deadline)
        keyword = region.find(_STREAM_KEYWORD, position)
        if keyword < 0:
            return
        after = keyword + len(_STREAM_KEYWORD)
        position = after
        standalone = (
            (keyword == 0 or region[keyword - 1] in _KEYWORD_LEFT_BOUNDARY)
            and (after == len(region) or region[after] in _KEYWORD_RIGHT_BOUNDARY)
        )
        if not standalone and region[keyword - 1:keyword] not in (b"\r", b"\n"):
            floor = max(0, keyword - _DICTIONARY_WINDOW)
            end = _skip_whitespace_back(region, keyword, floor)
            if end - 2 < floor or region[end - 2:end] != b">>":
                continue
        while after < len(region) and region[after] in _INLINE_WHITESPACE:
            after += 1
        if region.startswith(b"\r\n", after):
            data_start = after + 2
        elif region[after:after + 1] in (b"\r", b"\n"):
            data_start = after + 1
        else:
            position = after
            yield keyword, None, None
            continue
        data_end = region.find(_ENDSTREAM_KEYWORD, data_start)
        if data_end < 0:
            yield keyword, data_start, -1
            return
        yield keyword, data_start, data_end
        # A standalone ``stream`` inside the data means the keyword just matched
        # may sit in a literal string; resume at the inner one so the real stream
        # is still examined (the caller flags the outer span as ambiguous).
        inner = _inner_stream_keyword(region, data_start, data_end)
        position = inner if inner >= 0 else data_end + len(_ENDSTREAM_KEYWORD)


def _is_escaped(region, index, floor):
    backslashes = 0
    while index - backslashes > floor and region[index - backslashes - 1] == 0x5C:
        backslashes += 1
    return backslashes % 2 == 1


def _literal_string_start(region, close, floor):
    """Return the index of the ``(`` that opens the literal string closed at ``close``."""
    depth = 1
    index = close
    while index > floor:
        index -= 1
        byte = region[index]
        if byte in (0x28, 0x29) and not _is_escaped(region, index, floor):
            depth += 1 if byte == 0x29 else -1
            if depth == 0:
                return index
    return None


def _stream_dictionary(region, keyword):
    """Return the dictionary that directly precedes a ``stream`` keyword, or None.

    ``<<``/``>>`` are balanced backwards from the keyword, skipping literal
    strings.  None means the dictionary cannot be located unambiguously: no
    ``>>`` before the keyword, no balancing ``<<`` within the window, a stray
    ``(``, or a ``%`` that could open a comment hiding delimiters.
    """
    floor = max(0, keyword - _DICTIONARY_WINDOW)
    end = _skip_whitespace_back(region, keyword, floor)
    if end - 2 < floor or region[end - 2:end] != b">>":
        return None
    depth = 0
    index = end
    start = None
    while index > floor:
        byte = region[index - 1]
        pair = index - 2 >= floor and region[index - 2] == byte
        if byte == 0x3E and pair:
            depth += 1
            index -= 2
        elif byte == 0x3C and pair:
            depth -= 1
            index -= 2
            if depth == 0:
                start = index
                break
        elif byte == 0x29 and not _is_escaped(region, index - 1, floor):
            index = _literal_string_start(region, index - 1, floor)
            if index is None:
                return None
        elif byte == 0x28 and not _is_escaped(region, index - 1, floor):
            return None
        else:
            index -= 1
    if start is None:
        return None
    line_start = max(region.rfind(b"\n", floor, start), region.rfind(b"\r", floor, start)) + 1
    if line_start == 0 and floor > 0:
        return None
    if b"%" in region[line_start:end]:
        return None
    return region[start:end]


def _flate_decode(data, limit):
    decoder = zlib.decompressobj()
    try:
        output = decoder.decompress(data, limit + 1)
    except zlib.error as exc:
        raise PdfRiskError("invalid FlateDecode stream") from exc
    if len(output) > limit or decoder.unconsumed_tail:
        return output[:limit], False
    try:
        tail = decoder.flush(limit + 1 - len(output))
    except zlib.error as exc:
        raise PdfRiskError("invalid FlateDecode stream") from exc
    output += tail
    if decoder.unused_data.strip(b"\x00\t\r\n\x0c "):
        raise PdfRiskError("concatenated FlateDecode members are unsupported")
    return output[:limit], bool(decoder.eof and len(output) <= limit)


def inspect_pdf(
    path,
    *,
    max_scan_bytes=DEFAULT_MAX_SCAN_BYTES,
    max_decoded_bytes=DEFAULT_MAX_DECODED_BYTES,
    max_streams=DEFAULT_MAX_STREAMS,
    max_seconds=DEFAULT_MAX_SECONDS,
    extra_roots="",
):
    """Inspect a PDF for active-content indicators without rendering or execution."""
    scan_cap = _clamp_int(max_scan_bytes, DEFAULT_MAX_SCAN_BYTES, 1024, MAX_SCAN_BYTES)
    decode_cap = _clamp_int(max_decoded_bytes, DEFAULT_MAX_DECODED_BYTES, 1024, MAX_DECODED_BYTES)
    stream_cap = _clamp_int(max_streams, DEFAULT_MAX_STREAMS, 1, MAX_STREAMS)
    deadline = time.monotonic() + _clamp_seconds(max_seconds)
    with _open_guarded(str(path), extra_roots) as (actual, handle, opened):
        size = opened.st_size
        if size <= 0:
            raise PdfRiskError("PDF is empty")
        if size > MAX_SOURCE_BYTES:
            raise PdfRiskError("PDF exceeds the 256 MiB source ceiling")
        _deadline_check(deadline)
        complete = size <= scan_cap
        if complete:
            raw = handle.read(size)
            scan_regions = [raw]
            ranges = [[0, size]]
        else:
            prefix_size = scan_cap // 2
            suffix_size = scan_cap - prefix_size
            prefix = handle.read(prefix_size)
            handle.seek(max(prefix_size, size - suffix_size))
            suffix_start = handle.tell()
            suffix = handle.read(suffix_size)
            scan_regions = [prefix, suffix]
            raw = prefix + suffix
            ranges = [[0, len(prefix)], [suffix_start, suffix_start + len(suffix)]]
        _deadline_check(deadline)
        if not raw.startswith(b"%PDF-"):
            raise PdfRiskError("file does not begin with a PDF header")
        digest = hashlib.sha256(raw).hexdigest() if complete else None

    findings = {}
    for region in scan_regions:
        _merge_features(findings, _feature_counts(region), "raw")
        _deadline_check(deadline)
    incomplete_reasons = [] if complete else ["file_exceeds_scan_budget"]
    if any(
        re.search(br"/Encrypt(?![A-Za-z0-9])", _normalize_names(region))
        for region in scan_regions
    ):
        incomplete_reasons.append("encrypted_content")
    streams_seen = 0
    streams_decoded = 0
    decoded_total = 0
    unsupported_filter_count = 0
    malformed_streams = 0
    stream_spans = (
        (region, span)
        for region in scan_regions
        for span in _stream_spans(region, deadline)
    )
    for region, (keyword, data_start, data_end) in stream_spans:
        _deadline_check(deadline)
        if data_start is None:
            if "stream_keyword_without_eol" not in incomplete_reasons:
                incomplete_reasons.append("stream_keyword_without_eol")
            continue
        if data_end < 0:
            incomplete_reasons.append("unterminated_stream")
            continue
        streams_seen += 1
        if streams_seen > stream_cap:
            incomplete_reasons.append("stream_count_limit")
            break
        dictionary = _stream_dictionary(region, keyword)
        if dictionary is None:
            incomplete_reasons.append("ambiguous_stream_dictionary")
            continue
        normalized_dictionary = _normalize_names(dictionary)
        if len(_FILTER_RE.findall(normalized_dictionary)) > 1:
            incomplete_reasons.append("ambiguous_stream_dictionary")
            continue
        stream_data = region[data_start:data_end]
        if _inner_stream_keyword(region, data_start, data_end) >= 0:
            incomplete_reasons.append("ambiguous_stream_boundary")
        filters = _filters(normalized_dictionary)
        if not filters and _FILTER_KEY_RE.search(normalized_dictionary):
            # /Filter present but its value is not a name or array (e.g. an
            # indirect reference): the data may be compressed, never call it plain.
            unsupported_filter_count += 1
            incomplete_reasons.append("unsupported_stream_filter")
            continue
        if not filters:
            decoded = stream_data
            decoded_ok = True
        elif filters in (["FlateDecode"], ["Fl"]):
            remaining = decode_cap - decoded_total
            if remaining <= 0:
                incomplete_reasons.append("decoded_byte_limit")
                break
            try:
                decoded, decoded_ok = _flate_decode(stream_data, remaining)
            except PdfRiskError:
                malformed_streams += 1
                incomplete_reasons.append("malformed_flate_stream")
                _deadline_check(deadline)
                continue
            _deadline_check(deadline)
            streams_decoded += 1
        else:
            unsupported_filter_count += max(1, len(filters))
            incomplete_reasons.append("unsupported_stream_filter")
            continue
        decoded_total += len(decoded)
        _merge_features(findings, _feature_counts(decoded), "decoded_stream")
        _deadline_check(deadline)
        if not decoded_ok:
            incomplete_reasons.append("decoded_byte_limit")
        if decoded_total >= decode_cap:
            incomplete_reasons.append("decoded_byte_limit")
            break

    complete = complete and not incomplete_reasons
    ordered = [
        {"feature": name, **findings[name]}
        for name in sorted(findings)
    ]
    severities = {item["severity"] for item in ordered}
    if "high" in severities:
        risk = "high"
    elif "medium" in severities:
        risk = "medium"
    elif "low" in severities:
        risk = "low"
    else:
        risk = "none_detected" if complete else "unknown"
    return {
        "schema_version": 1,
        "path": str(actual),
        "source_bytes": size,
        "bytes_scanned": len(raw),
        "sha256": digest,
        "scan_complete": complete,
        "risk": risk,
        "findings": ordered,
        "incomplete_reasons": sorted(set(incomplete_reasons)),
        "ranges_scanned": ranges,
        "streams_seen": streams_seen,
        "streams_decoded": streams_decoded,
        "decoded_bytes": decoded_total,
        "unsupported_filter_count": unsupported_filter_count,
        "malformed_streams": malformed_streams,
        "execution": "none",
    }


def format_result(result):
    return json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
