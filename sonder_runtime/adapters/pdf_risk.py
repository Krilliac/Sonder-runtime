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
_STREAM_RE = re.compile(br"(?:\r\n|\r|\n)stream(?:\r\n|\r|\n)(.*?)endstream", re.S)
_NAME_ESCAPE_RE = re.compile(br"#([0-9A-Fa-f]{2})")
_FILTER_RE = re.compile(br"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9#]+)", re.S)
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


def _flate_decode(data, limit):
    decoder = zlib.decompressobj()
    output = decoder.decompress(data, limit + 1)
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
    stream_matches = (
        (region, match)
        for region in scan_regions
        for match in _STREAM_RE.finditer(region)
    )
    for region, match in stream_matches:
        _deadline_check(deadline)
        streams_seen += 1
        if streams_seen > stream_cap:
            incomplete_reasons.append("stream_count_limit")
            break
        dictionary = region[max(0, match.start() - 8192):match.start()]
        dictionary = dictionary[dictionary.rfind(b"<<"):]
        filters = _filters(_normalize_names(dictionary))
        stream_data = match.group(1)
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
