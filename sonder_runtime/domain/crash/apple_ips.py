"""macOS/iOS ``.ips`` crash reports (JSON, macOS 12+).

An ``.ips`` file is a one-line JSON header followed by a JSON body. Both go
through ``bounded_json.loads_bounded`` (byte cap, depth 64, int-string limit
caught). Every field is type-checked before use because the whole document
is untrusted.
"""
from __future__ import annotations

import re

from ..common.bounded_json import loads_bounded
from ..common.errors import InvalidInput
from .hints import finalize_report, is_system_module
from .model import (
    MAX_CRASHING_FRAMES, CaptureFormatError, CrashException, CrashReport, FrameTrust, ModuleInfo,
    StackFrame, ThreadSummary, module_basename,
)


MAX_IPS_BYTES = 8 * 1024 * 1024
MAX_THREADS = 512
MAX_IMAGES = 4096
_AT_ADDRESS_RE = re.compile(r"at (0x[0-9a-fA-F]+)")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value, limit: int) -> list:
    return value[:limit] if isinstance(value, list) else []


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -(1 << 64) < value < (1 << 64) else None
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def parse_apple_ips(text: str, *, source_label: str = "", input_sha256: str = "",
                    input_bytes: int = 0) -> CrashReport:
    """Parse an ``.ips`` report. Raises ``CaptureFormatError``."""
    if not isinstance(text, str):
        raise CaptureFormatError("NOT_IPS", ".ips text must be str")
    if len(text) > MAX_IPS_BYTES:
        raise CaptureFormatError("LIMIT_EXCEEDED", ".ips report is larger than %d bytes" % MAX_IPS_BYTES)
    head, _, rest = text.lstrip("﻿").partition("\n")
    try:
        header = loads_bounded(head, max_bytes=MAX_IPS_BYTES)
        body = loads_bounded(rest, max_bytes=MAX_IPS_BYTES) if rest.strip() else header
    except InvalidInput as exc:
        raise CaptureFormatError("NOT_IPS", str(exc)) from None
    if not isinstance(header, dict) or not isinstance(body, dict):
        raise CaptureFormatError("NOT_IPS", ".ips header and body must be JSON objects")
    if "threads" not in body and "exception" not in body:
        raise CaptureFormatError("NOT_IPS", "no threads or exception in the .ips body")

    images = []
    for index, raw in enumerate(_list(body.get("usedImages"), MAX_IMAGES)):
        image = _dict(raw)
        path = _str(image.get("path"))
        name = _str(image.get("name")) or module_basename(path)
        images.append(ModuleInfo(
            name=name, path=path, base=_int(image.get("base")) or 0, size=_int(image.get("size")) or 0,
            version=_str(image.get("CFBundleShortVersionString")), debug_id=_str(image.get("uuid")),
            in_project=bool(name) and not is_system_module(name, path) and _str(image.get("source")) != "S",
        ))
    faulting = _int(body.get("faultingThread"))
    threads = []
    crashing_id = None
    for index, raw in enumerate(_list(body.get("threads"), MAX_THREADS)):
        thread = _dict(raw)
        crashed = bool(thread.get("triggered")) or index == faulting
        frames = []
        for number, frame_raw in enumerate(_list(thread.get("frames"), MAX_CRASHING_FRAMES)):
            frame = _dict(frame_raw)
            image_index = _int(frame.get("imageIndex"))
            image = images[image_index] if image_index is not None and 0 <= image_index < len(images) else None
            offset = _int(frame.get("imageOffset"))
            frames.append(StackFrame(
                index=number,
                address=(image.base + offset) if image is not None and offset is not None else None,
                module=image.name if image else "", module_offset=offset,
                function=_str(frame.get("symbol")), file=_str(frame.get("sourceFile")),
                line=_int(frame.get("sourceLine")), inline=bool(frame.get("inline")),
                trust=FrameTrust.DEBUGGER.value, in_project=bool(image and image.in_project),
            ))
        tid = _int(thread.get("id")) or index
        if crashed and crashing_id is None:
            crashing_id = tid
        threads.append(ThreadSummary(thread_id=tid, name=_str(thread.get("name")) or _str(thread.get("queue")),
                                     crashed=crashed and crashing_id == tid, frames=tuple(frames)))
    crashing = [t for t in threads if t.crashed]
    others = [t for t in threads if not t.crashed]
    ordered = tuple(crashing[:1] + [ThreadSummary(t.thread_id, t.name, False, t.frames[:8],
                                                  len(t.frames) > 8) for t in others[:15]])

    exc_raw = _dict(body.get("exception"))
    exception = None
    if exc_raw:
        subtype = _str(exc_raw.get("subtype"))
        address = _AT_ADDRESS_RE.search(subtype)
        exc_type = _str(exc_raw.get("type"))
        signal = _str(exc_raw.get("signal"))
        exception = CrashException(
            code=_str(exc_raw.get("codes")), name=exc_type or signal, signal=signal,
            address=crashing[0].frames[0].address if crashing and crashing[0].frames else None,
            access_address=int(address.group(1), 16) if address else None,
            thread_id=crashing_id, detail=subtype,
        )
    os_version = _dict(body.get("osVersion"))
    notes = []
    termination = _dict(body.get("termination"))
    if termination:
        notes.append("termination: %s %s" % (_str(termination.get("namespace")),
                                               _str(termination.get("indicator"))))
    ordered_modules = sorted(images, key=lambda m: (not m.in_project,))[:128]
    report = CrashReport(
        source_kind="apple_ips", engines=("pure",), source_label=source_label,
        input_sha256=input_sha256, input_bytes=input_bytes,
        os=_str(os_version.get("train")) or _str(header.get("os_version")),
        cpu=_str(body.get("cpuType")), process_name=_str(body.get("procName")) or _str(header.get("app_name")),
        pid=_int(body.get("pid")), exception=exception, crashing_thread_id=crashing_id,
        threads=ordered, threads_total=len(threads), modules=tuple(ordered_modules),
        modules_total=len(images), notes=tuple(notes),
        truncated=len(threads) > len(ordered) or len(images) > len(ordered_modules),
    )
    return finalize_report(report)


__all__ = ["parse_apple_ips"]
