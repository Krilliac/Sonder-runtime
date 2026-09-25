"""Valgrind memcheck XML (``--xml=yes``, protocol 4) into a ``CrashReport``.

The document goes through ``domain.common.safe_xml`` (no DTD, no entities,
size-capped). The report's exception is the fatal signal when valgrind saw
one, otherwise the first error; its stack is the crashing thread, and the
first error's auxiliary stack (e.g. where the block was freed) becomes a
second thread.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..common.errors import InvalidInput
from ..common.safe_xml import local_tag, parse_guarded_xml
from ..diagnostics.model import clean_text
from .exceptions import FAULT_ADDRESS_SIGNALS, si_code_name
from .hints import finalize_report, is_system_file, is_system_module
from .model import (
    MAX_CRASHING_FRAMES, CaptureFormatError, CrashException, CrashReport, FrameTrust, ModuleInfo,
    StackFrame, ThreadSummary, module_basename,
)


MAX_XML_BYTES = 8 * 1024 * 1024
MAX_ERRORS_SCANNED = 10_000
_SIZE_RE = re.compile(r"(?P<rw>read|write) of size (?P<size>\d+)", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"Address (?P<addr>0x[0-9A-Fa-f]+)")


def _text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
    for child in element:
        if local_tag(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if local_tag(child.tag) == name]


def _int(value: str, base: int = 10) -> int | None:
    try:
        return int(value, base)
    except (TypeError, ValueError):
        return None


def _frames(stack: ET.Element | None, limit: int) -> tuple[StackFrame, ...]:
    if stack is None:
        return ()
    out = []
    for index, frame in enumerate(_children(stack, "frame")[:limit]):
        directory = _text(frame, "dir")
        file_ = _text(frame, "file")
        path = "%s/%s" % (directory.rstrip("/"), file_) if directory and file_ else file_
        obj = _text(frame, "obj")
        module = module_basename(obj)
        in_project = bool(path) and not is_system_file(path) and not (module and is_system_module(module, obj))
        out.append(StackFrame(
            index=index, address=_int(_text(frame, "ip"), 16), module=module,
            function=_text(frame, "fn"), file=path, line=_int(_text(frame, "line")),
            trust=FrameTrust.SANITIZER.value, in_project=in_project,
        ))
    return tuple(out)


def parse_valgrind_xml(data: bytes, *, source_label: str = "", input_sha256: str = "",
                       input_bytes: int = 0) -> CrashReport:
    """Parse memcheck XML. Raises ``CaptureFormatError``."""
    try:
        root = parse_guarded_xml(data, max_bytes=MAX_XML_BYTES)
    except InvalidInput as exc:
        raise CaptureFormatError("NOT_VALGRIND", str(exc)) from None
    if local_tag(root.tag) != "valgrindoutput":
        raise CaptureFormatError("NOT_VALGRIND", "root element is not <valgrindoutput>")
    tool = _text(root, "tool") or _text(root, "protocoltool")
    pid = _int(_text(root, "pid"))
    exe = ""
    for args in _children(root, "args"):
        for argv in _children(args, "argv"):
            exe = _text(argv, "exe")
    errors = _children(root, "error")[:MAX_ERRORS_SCANNED]
    fatal = next(iter(_children(root, "fatal_signal")), None)
    first = errors[0] if errors else None
    threads: list[ThreadSummary] = []
    exception: CrashException | None = None
    notes: list[str] = []
    if len(errors) > 1:
        notes.append("%d memcheck errors; the first is reported" % len(errors))
    if first is not None:
        kind = _text(first, "kind")
        what = _text(first, "what") or _text(next(iter(_children(first, "xwhat")), None), "text")
        size = _SIZE_RE.search(what)
        aux = _text(first, "auxwhat")
        address = _ADDRESS_RE.search(aux)
        exception = CrashException(
            code=kind, name=kind, access=(size.group("rw").lower() if size else ""),
            access_address=_int(address.group("addr"), 16) if address else None,
            thread_id=_int(_text(first, "tid")),
            detail=clean_text("%s; %s" % (what, aux) if aux else what, 240),
        )
        stacks = _children(first, "stack")
        threads.append(ThreadSummary(thread_id=_int(_text(first, "tid")) or 1, name=clean_text(what, 120),
                                     crashed=True, frames=_frames(stacks[0] if stacks else None,
                                                                  MAX_CRASHING_FRAMES)))
        if len(stacks) > 1:
            threads.append(ThreadSummary(thread_id=0, name=clean_text(aux, 120), crashed=False,
                                         frames=_frames(stacks[1], 8)))
    if fatal is not None:
        signame = _text(fatal, "signame") or "SIG%s" % _text(fatal, "signo")
        siaddr = _int(_text(fatal, "siaddr"), 16)
        sicode = _int(_text(fatal, "sicode")) or 0
        fault_frames = _frames(next(iter(_children(fatal, "stack")), None), MAX_CRASHING_FRAMES)
        tid = _int(_text(fatal, "tid")) or 1
        exception = CrashException(
            code=_text(fatal, "signo"), name=signame, signal=signame,
            address=fault_frames[0].address if fault_frames else None,
            access=exception.access if exception is not None else "",
            access_address=siaddr if signame in FAULT_ADDRESS_SIGNALS else None,
            thread_id=tid, detail=clean_text(_text(fatal, "event") or si_code_name(signame, sicode), 240),
        )
        if first is not None:
            notes.append("first memcheck error: %s" % clean_text(_text(first, "what"), 160))
        threads = [ThreadSummary(thread_id=tid, name="fatal signal", crashed=True, frames=fault_frames)] + [
            ThreadSummary(thread_id=t.thread_id, name=t.name, crashed=False, frames=t.frames[:8])
            for t in threads]
    if exception is None:
        raise CaptureFormatError("NOT_VALGRIND", "no memcheck error or fatal signal in the document")
    modules: dict[str, ModuleInfo] = {}
    for thread in threads:
        for frame in thread.frames:
            if frame.module and frame.module.lower() not in modules and len(modules) < 128:
                modules[frame.module.lower()] = ModuleInfo(name=frame.module,
                                                           in_project=not is_system_module(frame.module))
    report = CrashReport(
        source_kind="valgrind_xml", engines=("pure",), source_label=source_label,
        input_sha256=input_sha256, input_bytes=input_bytes,
        process_name=module_basename(exe), pid=pid, exception=exception,
        crashing_thread_id=threads[0].thread_id if threads else None, threads=tuple(threads[:16]),
        threads_total=len(threads), modules=tuple(modules.values()), modules_total=len(modules),
        notes=tuple(notes + (["tool %s" % tool] if tool and tool != "memcheck" else [])),
    )
    return finalize_report(report)


__all__ = ["parse_valgrind_xml"]
