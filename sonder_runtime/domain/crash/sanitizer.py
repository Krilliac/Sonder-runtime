"""Sanitizer report text (ASan, HWASan, MSVC ASan, LSan, TSan, MSan, UBSan).

The first report in the text is parsed into a ``CrashReport``:

- the report header gives the sanitizer, the kind (``heap-use-after-free``,
  ``data race``, ``detected memory leaks``, UBSan ``runtime error``...), the
  address and the reporting pid;
- ``READ/WRITE of size N at ADDR thread T0`` (or TSan's ``Read of size``)
  gives the access;
- the first stack is the crashing thread; later stacks (``freed by thread
  T0 here:``, ``previously allocated by``, TSan's ``Previous write``, leak
  stacks) become extra threads named after their section header.

Frames look like ``#0 0x55d4 in func /path/file.cpp:12:5`` (ASan),
``#0 func /path/file.cpp:6 (bin+0x13c7) (BuildId: ...)`` (TSan) or
``#4 0x5 in _start (/path/bin+0x1284)``. Text is bounded (8 MiB window,
200k lines, 64 frames per stack, 16 stacks) and every string is cleaned.
"""
from __future__ import annotations

import re

from ..diagnostics.model import clean_text
from .hints import finalize_report, is_system_file, is_system_module
from .model import (
    MAX_CRASHING_FRAMES, CaptureFormatError, CrashException, CrashReport, FrameTrust, ModuleInfo,
    StackFrame, ThreadSummary, module_basename,
)


MAX_TEXT_CHARS = 8 * 1024 * 1024
MAX_LINES = 200_000
MAX_STACKS = 16
MAX_OTHER_STACK_FRAMES = 8

_ERROR_RE = re.compile(
    r"^==(?P<pid>\d+)==\s*ERROR: (?P<san>AddressSanitizer|HWAddressSanitizer|LeakSanitizer"
    r"|MemorySanitizer|KernelAddressSanitizer): (?P<msg>.+)$")
_WARNING_RE = re.compile(
    r"^(?:==(?P<pid>\d+)==\s*)?WARNING: (?P<san>ThreadSanitizer|MemorySanitizer): (?P<msg>.+)$")
_PID_SUFFIX_RE = re.compile(r" \(pid=(?P<pid>\d+)\)")
_UBSAN_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<msg>.+)$")
_UBSAN_WIN_RE = re.compile(
    r"^(?P<file>[A-Za-z]:\\[^:]*?):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<msg>.+)$")
_FRAME_RE = re.compile(r"^\s*#(?P<index>\d+)\s+(?:(?P<addr>0x[0-9a-fA-F]+)\s+(?:in\s+)?)?(?P<rest>.*)$")
# Suffix patterns are only ever full-matched against the text after the last
# "(" of a frame line (see ``_split_suffix``): a leading ``\s*`` in a
# ``search`` would retry at every blank of a hostile line (quadratic).
_BUILD_ID_RE = re.compile(r"\(BuildId: [0-9a-fA-F]+\)")
_MODULE_RE = re.compile(r"\((?P<module>[^()]+?)\+(?P<offset>0x[0-9a-fA-F]+)\)")
_LOCATION_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?$")
_ACCESS_RE = re.compile(
    r"^(?:==\d+==\s*)?(?P<kind>READ|WRITE|Read|Write|Atomic read|Atomic write|Previous read"
    r"|Previous write|Previous atomic read|Previous atomic write) of size (?P<size>\d+) at "
    r"(?P<addr>0x[0-9a-fA-F]+)(?: thread (?P<thread>T\d+)| by (?P<by>.+))?:?\s*$")
_SIGNAL_ACCESS_RE = re.compile(r"The signal is caused by a (?P<kind>READ|WRITE|UNKNOWN) memory access")
_ADDRESS_RE = re.compile(r"on (?:unknown )?address (?P<addr>0x[0-9a-fA-F]+)")
_PC_RE = re.compile(r"\bpc (?P<pc>0x[0-9a-fA-F]+)")
_THREAD_ID_RE = re.compile(r"\b(?:thread )?T(?P<tid>\d+)\b")
_SUMMARY_RE = re.compile(r"^SUMMARY: (?P<san>\w+): (?P<rest>.+)$")


def _kind_from_message(sanitizer: str, message: str) -> str:
    text = message.strip()
    if sanitizer == "LeakSanitizer" or text.startswith("detected memory leaks"):
        return "memory-leak"
    if sanitizer == "ThreadSanitizer":
        base = re.split(r" \(|:", text, maxsplit=1)[0].strip()
        return base.replace(" ", "-")
    match = re.match(r"^(?P<kind>attempting [\w-]+|[A-Za-z][\w-]*)", text)
    return match.group("kind") if match else clean_text(text, 64)


def _split_suffix(text: str, pattern: re.Pattern) -> tuple[str, re.Match | None]:
    """``(head, match)`` when ``text`` ends with a parenthesized ``pattern``.

    Linear: only the text after the last "(" is matched, once.
    """
    if not text.endswith(")"):
        return text, None
    start = text.rfind("(")
    found = pattern.fullmatch(text, start) if start >= 0 else None
    if found is None:
        return text, None
    return text[:start].rstrip(), found


def _parse_frame(line: str, *, trust: str) -> StackFrame | None:
    match = _FRAME_RE.match(line)
    if match is None:
        return None
    rest, _ = _split_suffix(match.group("rest").strip(), _BUILD_ID_RE)
    module = ""
    offset = None
    rest, found = _split_suffix(rest, _MODULE_RE)
    if found is not None:
        module = module_basename(found.group("module"))
        offset = int(found.group("offset"), 16)
    file_ = ""
    line_no = None
    col = None
    function = rest
    if rest.endswith(" <null>"):
        function = rest[: -len(" <null>")]
    else:
        head, sep, tail = rest.rpartition(" ")
        location = _LOCATION_RE.match(tail) if sep else None
        if location is not None and ("/" in tail or "\\" in tail or "." in location.group("file")):
            file_ = location.group("file")
            line_no = int(location.group("line"))
            col = int(location.group("col")) if location.group("col") else None
            function = head
    function = function.strip()
    if function in ("<null>", "<unknown module>"):
        function = ""
    address = int(match.group("addr"), 16) if match.group("addr") else None
    in_project = bool(file_) and not is_system_file(file_) and not (module and is_system_module(module))
    return StackFrame(index=int(match.group("index")), address=address, module=module,
                      module_offset=offset, function=function, file=file_, line=line_no,
                      column=col, trust=trust, in_project=in_project)


def parse_sanitizer_report(text: str, *, source_label: str = "", input_sha256: str = "",
                           input_bytes: int = 0) -> CrashReport:
    """Parse the first sanitizer report in ``text``. Raises ``CaptureFormatError``."""
    if not isinstance(text, str):
        raise CaptureFormatError("NOT_SANITIZER", "sanitizer text must be str")
    window = text[:MAX_TEXT_CHARS]
    lines = window.splitlines()[:MAX_LINES]
    start = None
    sanitizer = ""
    message = ""
    pid = None
    ubsan = None
    for number, raw in enumerate(lines):
        line = raw.rstrip()
        match = _ERROR_RE.match(line) or _WARNING_RE.match(line)
        if match is not None:
            start, sanitizer, message = number, match.group("san"), match.group("msg")
            pid_text = match.group("pid") or ""
            if match.re is _WARNING_RE:
                message = message.rstrip()
                cut = message.rfind(" (pid=")
                suffix = _PID_SUFFIX_RE.fullmatch(message, cut) if cut > 0 else None
                if suffix is not None:
                    message = message[:cut]
                    pid_text = pid_text or suffix.group("pid")
            pid = int(pid_text) if pid_text else None
            break
        match = _UBSAN_RE.match(line) or _UBSAN_WIN_RE.match(line)
        if match is not None:
            start, sanitizer, message, ubsan = number, "UndefinedBehaviorSanitizer", match.group("msg"), match
            break
    if start is None:
        raise CaptureFormatError("NOT_SANITIZER", "no sanitizer report header found")
    kind = "undefined-behavior" if ubsan is not None else _kind_from_message(sanitizer, message)
    address = None
    found = _ADDRESS_RE.search(message)
    if found is not None:
        address = int(found.group("addr"), 16)
    pc = None
    found = _PC_RE.search(message)
    if found is not None:
        pc = int(found.group("pc"), 16)
    access = ""
    access_detail = ""
    stacks: list[tuple[str, list[StackFrame], bool]] = []
    current: list[StackFrame] | None = None
    header = "crashing stack"
    truncated = False
    notes: list[str] = []
    others = 0
    for raw in lines[start + 1:]:
        line = raw.rstrip()
        stripped = line.strip()
        if _ERROR_RE.match(line) or _WARNING_RE.match(line) or (
                ubsan is not None and (_UBSAN_RE.match(line) or _UBSAN_WIN_RE.match(line))):
            others += 1
            break
        if stripped.startswith("SUMMARY:") or stripped.startswith("Shadow bytes around"):
            if stripped.startswith("SUMMARY:"):
                continue
            break
        access_match = _ACCESS_RE.match(stripped)
        if access_match is not None and not access_match.group("kind").startswith("Previous"):
            if not access:
                access = "write" if "write" in access_match.group("kind").lower() else "read"
                access_detail = "%s of size %s" % (access_match.group("kind"), access_match.group("size"))
                if address is None:
                    address = int(access_match.group("addr"), 16)
            current = None
            continue
        signal_access = _SIGNAL_ACCESS_RE.search(stripped)
        if signal_access is not None:
            access = signal_access.group("kind").lower() if signal_access.group("kind") != "UNKNOWN" else ""
            continue
        frame = _parse_frame(line, trust=FrameTrust.SANITIZER.value) if stripped.startswith("#") else None
        if frame is not None:
            if current is None:
                if len(stacks) >= MAX_STACKS:
                    truncated = True
                    continue
                current = []
                stacks.append((header, current, False))
            if len(current) < MAX_CRASHING_FRAMES:
                current.append(frame)
            else:
                truncated = True
            continue
        current = None
        if stripped.endswith(":") and stacks:
            header = stripped[:-1].strip()
    if others:
        notes.append("further sanitizer reports follow; only the first is parsed")

    threads: list[ThreadSummary] = []
    for index, (title, frames, _) in enumerate(stacks):
        if index == 0:
            threads.append(ThreadSummary(thread_id=0, name="sanitizer report", crashed=True,
                                         frames=tuple(frames)))
        else:
            tid_match = _THREAD_ID_RE.search(title)
            frames = frames[:MAX_OTHER_STACK_FRAMES]
            threads.append(ThreadSummary(thread_id=int(tid_match.group("tid")) if tid_match else index,
                                         name=title, crashed=False, frames=tuple(frames),
                                         frames_truncated=len(stacks[index][1]) > len(frames)))
    if ubsan is not None and not stacks:
        frame = StackFrame(index=0, file=ubsan.group("file"), line=int(ubsan.group("line")),
                           column=int(ubsan.group("col")), trust=FrameTrust.SANITIZER.value,
                           in_project=not is_system_file(ubsan.group("file")))
        threads.append(ThreadSummary(thread_id=0, name="sanitizer report", crashed=True, frames=(frame,)))

    modules: dict[str, ModuleInfo] = {}
    for thread in threads:
        for frame in thread.frames:
            if frame.module and frame.module.lower() not in modules and len(modules) < 128:
                modules[frame.module.lower()] = ModuleInfo(
                    name=frame.module, symbols="loaded" if frame.function else "not_attempted",
                    in_project=not is_system_module(frame.module))
    process_name = next((m.name for m in modules.values() if m.in_project), "")
    signal = "SIGSEGV" if kind in ("SEGV",) else ("SIGBUS" if kind == "BUS" else "")
    detail = access_detail or (clean_text(message, 240) if ubsan is not None or sanitizer == "ThreadSanitizer" else "")
    exception = CrashException(
        code=sanitizer, name=kind, signal=signal, address=pc,
        access=access, access_address=address, thread_id=0 if threads else None, detail=detail,
    )
    report = CrashReport(
        source_kind="sanitizer_report", engines=("pure",), source_label=source_label,
        input_sha256=input_sha256, input_bytes=input_bytes, process_name=process_name, pid=pid,
        exception=exception, crashing_thread_id=0 if threads else None, threads=tuple(threads),
        threads_total=len(threads), modules=tuple(modules.values()), modules_total=len(modules),
        notes=tuple(notes), truncated=truncated or len(text) > MAX_TEXT_CHARS,
    )
    return finalize_report(report)


__all__ = ["parse_sanitizer_report"]
