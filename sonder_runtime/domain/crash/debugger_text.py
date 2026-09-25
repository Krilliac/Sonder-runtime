"""Parsers for Tier-1 debugger and symbolizer output.

The debuggers print text that the crashed process controls (thread names,
module paths, strings in frames). Section boundaries are therefore trusted
only when they carry the per-run nonce the launcher bound into the argv:

- cdb and gdb: a line that is exactly ``SONDER_<nonce>_<SECTION>``;
- lldb: an echo line starting with the prompt ``(SONDER_<nonce>)``.

Text outside nonce sections is ignored, so a dump containing
``SONDER_<other>_BT`` or a thread name containing ``\\n(lldb) image list``
cannot open a section. Every parser is linear, bounded (2 MB window, 4096
frames, 1024 threads) and returns ``DebuggerFindings``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ..common.bounded_json import loads_bounded
from ..common.errors import InvalidInput
from .exceptions import ACCESS_KINDS, fast_fail_name, ntstatus_name
from .model import CaptureFormatError, CrashException, FrameTrust, ModuleInfo, StackFrame, module_basename


MAX_TEXT_CHARS = 2 * 1024 * 1024
MAX_FRAMES_TOTAL = 4096
MAX_THREADS = 1024
MAX_FRAMES_PER_THREAD = 128
MAX_MODULES = 4096
MAX_LINE_CHARS = 8192
_NONCE_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True, slots=True)
class SymbolizedAddress:
    module: str
    offset: int
    frames: tuple[StackFrame, ...]


@dataclass(frozen=True, slots=True)
class DebuggerFindings:
    frames_by_thread: tuple[tuple[int, tuple[StackFrame, ...]], ...] = ()
    crashing_thread: int | None = None
    exception: CrashException | None = None
    modules: tuple[ModuleInfo, ...] = ()
    sections_seen: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    symbolized: tuple[SymbolizedAddress, ...] = ()
    thread_names: tuple[tuple[int, str], ...] = ()

    def frames_for(self, thread_id: int | None) -> tuple[StackFrame, ...]:
        for tid, frames in self.frames_by_thread:
            if tid == thread_id:
                return frames
        return ()


def _check_nonce(nonce: str) -> str:
    text = str(nonce or "")
    if not _NONCE_RE.match(text):
        raise InvalidInput("nonce must be 16 lower-case hex digits")
    return text


def _window(text: str) -> list[str]:
    if not isinstance(text, str):
        raise CaptureFormatError("PARSE_FAILED", "debugger output must be text")
    return [line[:MAX_LINE_CHARS] for line in text[:MAX_TEXT_CHARS].splitlines()]


# " at <file>:<line>[:<col>]" suffixes are located without regex search over
# the whole line: a lazy ``" at (\S.*?):(\d+)$"`` search restarts at every
# " at " and rescans to the end (quadratic on a hostile frame line).
_LINE_SUFFIX_RE = re.compile(r":(?P<line>\d+)$")
_LINE_COL_SUFFIX_RE = re.compile(r":(?P<line>\d+)(?::(?P<col>\d+))?$")


def _split_source(rest: str, *, with_column: bool) -> tuple[str, str, int, int | None] | None:
    """``(head, file, line, col)`` for the first `` at <file>:<line>`` suffix, else None."""
    suffix = (_LINE_COL_SUFFIX_RE if with_column else _LINE_SUFFIX_RE).search(rest)
    if suffix is None:
        return None
    limit = suffix.start()
    index = rest.find(" at ")
    while 0 <= index and index + 4 < limit:
        if not rest[index + 4].isspace():
            col = suffix.groupdict().get("col")
            return (rest[:index], rest[index + 4:limit], int(suffix.group("line")),
                    int(col) if col else None)
        index = rest.find(" at ", index + 1)
    return None


def _marker_sections(text: str, nonce: str) -> tuple[dict[str, list[str]], list[str]]:
    """Split on lines that are exactly ``SONDER_<nonce>_<NAME>``."""
    marker = re.compile(r"^SONDER_%s_(?P<name>[A-Z]{2,16})$" % _check_nonce(nonce))
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: list[str] | None = None
    for raw in _window(text):
        line = raw.rstrip("\r")
        match = marker.match(line.strip())
        if match is not None:
            name = match.group("name")
            if name == "END":
                current = None
                order.append(name)
                continue
            current = sections.setdefault(name, [])
            order.append(name)
            continue
        if current is not None:
            current.append(line)
    return sections, order


def _hex(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace("`", ""), 16)
    except ValueError:
        return None


def _cut_args(text: str) -> str:
    """``ns::f(int, char)`` / ``f(x=1)`` -> the name before its argument list."""
    value = text.strip()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "(":
            if value.startswith("(anonymous namespace)", index):
                index += len("(anonymous namespace)")
                continue
            if value[:index].endswith("operator"):
                close = value.find(")", index)
                index = close + 1 if close > 0 else len(value)
                continue
            return value[:index].rstrip()
        index += 1
    return value


# --------------------------------------------------------------------- gdb

_GDB_FRAME_RE = re.compile(r"^#(?P<n>\d+)\s+(?:(?P<addr>0x[0-9a-fA-F]+) in )?(?P<rest>.+)$")
_GDB_FROM_RE = re.compile(r" from (?P<lib>\S.*)$")
_GDB_THREAD_RE = re.compile(
    r"^Thread (?P<num>\d+) \((?:Thread 0x[0-9a-fA-F]+ \()?(?:LWP|process) (?P<lwp>\d+)\)?"
    r"(?: \"(?P<name>[^\"]*)\")?\):?\s*$")
_GDB_LIB_RE = re.compile(
    r"^(?:(?P<start>0x[0-9a-fA-F]+)\s+(?P<end>0x[0-9a-fA-F]+)\s+)?(?P<read>Yes \(\*\)|Yes|No)\s+(?P<path>\S.*)$")
_GDB_SIG_RE = re.compile(r"^\$\d+ = \(void \*\) (?P<addr>0x[0-9a-fA-F]+)")


def _gdb_frame(line: str) -> StackFrame | None:
    match = _GDB_FRAME_RE.match(line.strip())
    if match is None:
        return None
    rest = match.group("rest")
    file_ = ""
    line_no = None
    module = ""
    at = _split_source(rest, with_column=False)
    if at is not None:
        rest, file_, line_no, _ = at
    else:
        lib = _GDB_FROM_RE.search(rest)
        if lib is not None:
            module = module_basename(lib.group("lib"))
            rest = rest[:lib.start()]
    function = _cut_args(rest)
    if function in ("??", ""):
        function = ""
    address = _hex(match.group("addr"))
    return StackFrame(index=int(match.group("n")), address=address, module=module, function=function,
                      file=file_, line=line_no, inline=address is None and int(match.group("n")) > 0,
                      trust=FrameTrust.DEBUGGER.value)


def _frames(lines: list[str], parse, budget: list[int]) -> list[StackFrame]:
    out = []
    for line in lines:
        if budget[0] <= 0 or len(out) >= MAX_FRAMES_PER_THREAD:
            break
        frame = parse(line)
        if frame is not None:
            out.append(frame)
            budget[0] -= 1
    return out


def parse_gdb(text: str, nonce: str) -> DebuggerFindings:
    sections, order = _marker_sections(text, nonce)
    budget = [MAX_FRAMES_TOTAL]
    notes: list[str] = []
    crash_frames = _frames(sections.get("BT", []), _gdb_frame, budget)
    threads: list[tuple[int, tuple[StackFrame, ...]]] = []
    names: list[tuple[int, str]] = []
    current_tid = None
    current: list[str] = []

    def flush() -> None:
        if current_tid is not None and len(threads) < MAX_THREADS:
            threads.append((current_tid, tuple(_frames(current, _gdb_frame, budget))))

    for line in sections.get("THREADS", []):
        header = _GDB_THREAD_RE.match(line.strip())
        if header is not None:
            flush()
            current_tid = int(header.group("lwp"))
            current = []
            if header.group("name"):
                names.append((current_tid, header.group("name")))
            continue
        current.append(line)
    flush()
    crashing = None
    if crash_frames:
        top = crash_frames[0]
        for tid, frames in threads:
            if frames and frames[0].address == top.address and frames[0].function == top.function:
                crashing = tid
                break
        if crashing is None:
            crashing = threads[0][0] if threads else 0
            notes.append("crashing thread inferred")
        threads = [(tid, frames) for tid, frames in threads if tid != crashing]
        threads.insert(0, (crashing, tuple(crash_frames)))
    modules = []
    for line in sections.get("LIBS", []):
        match = _GDB_LIB_RE.match(line.strip())
        if match is None or len(modules) >= MAX_MODULES:
            continue
        path = match.group("path")
        start = _hex(match.group("start"))
        end = _hex(match.group("end"))
        modules.append(ModuleInfo(
            name=module_basename(path), path=path, base=start or 0,
            size=(end - start) if start is not None and end is not None and end > start else 0,
            symbols="loaded" if match.group("read") == "Yes" else "not_found"))
    exception = None
    for line in sections.get("SIG", []):
        match = _GDB_SIG_RE.match(line.strip())
        if match is not None:
            exception = CrashException(access_address=int(match.group("addr"), 16))
            break
    return DebuggerFindings(frames_by_thread=tuple(threads), crashing_thread=crashing, exception=exception,
                            modules=tuple(modules), sections_seen=tuple(order), notes=tuple(notes),
                            thread_names=tuple(names))


# --------------------------------------------------------------------- lldb

_LLDB_THREAD_RE = re.compile(
    r"^(?P<star>\*)?\s*thread #(?P<num>\d+)(?:, tid = (?P<tid>0x[0-9a-fA-F]+|\d+))?"
    r"(?:, name = '(?P<name>[^']*)')?(?:, queue = '[^']*')?(?:, stop reason = (?P<reason>.*))?$")
_LLDB_FRAME_RE = re.compile(
    r"^\*?\s*frame #(?P<n>\d+): (?P<addr>0x[0-9a-fA-F]+)(?: (?P<module>[^`\s]+)`(?P<rest>.*))?"
    r"(?: (?P<bare>[^`\s]+))?$")
_LLDB_IMAGE_RE = re.compile(
    r"^\[\s*(?P<idx>\d+)\]\s+(?:(?P<uuid>[0-9A-Fa-f-]{8,})\s+)?(?P<base>0x[0-9a-fA-F]+)\s+(?P<path>\S.*)$")
_HEX_IN_PARENS_RE = re.compile(r"\(0x[0-9a-fA-F]+\)")
_LLDB_SIGNAL_RE = re.compile(r"signal (?P<sig>SIG[A-Z0-9]+)(?:: (?P<detail>[^(]*))?(?:\(fault address: (?P<addr>0x[0-9a-fA-F]+)\))?")


def _lldb_frame(line: str) -> StackFrame | None:
    match = _LLDB_FRAME_RE.match(line.strip())
    if match is None:
        return None
    module = match.group("module") or match.group("bare") or ""
    rest = match.group("rest") or ""
    inline = "[inlined]" in rest
    rest = rest.replace("[inlined] ", "")
    file_ = ""
    line_no = None
    col = None
    at = _split_source(rest, with_column=True)
    if at is not None:
        rest, file_, line_no, col = at
    rest = re.sub(r" \+ \d+$", "", rest)
    function = _cut_args(rest)
    if function.startswith("___lldb_unnamed_symbol"):
        function = ""
    return StackFrame(index=int(match.group("n")), address=_hex(match.group("addr")), module=module,
                      function=function, file=file_, line=line_no, column=col, inline=inline,
                      trust=FrameTrust.DEBUGGER.value)


def _prompt_sections(text: str, nonce: str) -> tuple[dict[str, list[str]], list[str]]:
    prompt = re.compile(r"^\(SONDER_%s\) ?(?P<cmd>.*)$" % _check_nonce(nonce))
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: list[str] | None = None
    for raw in _window(text):
        line = raw.rstrip("\r")
        match = prompt.match(line)
        if match is not None:
            command = match.group("cmd").strip()
            key = "backtrace" if command.startswith("thread backtrace") else (
                "images" if command.startswith("image list") else command[:40])
            current = sections.setdefault(key, []) if key in ("backtrace", "images") else None
            order.append(key)
            continue
        if current is not None:
            current.append(line)
    return sections, order


def parse_lldb(text: str, nonce: str) -> DebuggerFindings:
    sections, order = _prompt_sections(text, nonce)
    budget = [MAX_FRAMES_TOTAL]
    threads: list[tuple[int, tuple[StackFrame, ...]]] = []
    names: list[tuple[int, str]] = []
    crashing = None
    exception = None
    current_tid = None
    current: list[str] = []

    def flush() -> None:
        if current_tid is not None and len(threads) < MAX_THREADS:
            threads.append((current_tid, tuple(_frames(current, _lldb_frame, budget))))

    for line in sections.get("backtrace", []):
        header = _LLDB_THREAD_RE.match(line.strip())
        if header is not None:
            flush()
            tid_text = header.group("tid")
            current_tid = int(tid_text, 0) if tid_text else int(header.group("num"))
            current = []
            if header.group("name"):
                names.append((current_tid, header.group("name")))
            reason = header.group("reason") or ""
            if header.group("star") and crashing is None:
                crashing = current_tid
                signal = _LLDB_SIGNAL_RE.search(reason)
                if signal is not None:
                    exception = CrashException(
                        name=signal.group("sig"), signal=signal.group("sig"),
                        access_address=_hex(signal.group("addr")),
                        detail=(signal.group("detail") or "").strip())
            continue
        current.append(line)
    flush()
    modules = []
    for line in sections.get("images", []):
        match = _LLDB_IMAGE_RE.match(line.strip())
        if match is None or len(modules) >= MAX_MODULES:
            continue
        path = match.group("path")
        # Drop a trailing "(0x...)" slide without a lazy regex (quadratic on
        # hostile whitespace runs): match only the text after the last "(".
        cut = path.rfind("(")
        if cut > 0 and path[cut - 1].isspace() and _HEX_IN_PARENS_RE.fullmatch(path, cut):
            path = path[:cut].rstrip()
        modules.append(ModuleInfo(name=module_basename(path), path=path, base=_hex(match.group("base")) or 0,
                                  debug_id=(match.group("uuid") or "").replace("-", "").lower(),
                                  symbols="not_attempted"))
    if crashing is not None:
        threads.sort(key=lambda item: item[0] != crashing)
    return DebuggerFindings(frames_by_thread=tuple(threads), crashing_thread=crashing, exception=exception,
                            modules=tuple(modules), sections_seen=tuple(order), thread_names=tuple(names))


# --------------------------------------------------------------------- cdb

_CDB_FRAME_RE = re.compile(
    r"^(?P<n>[0-9a-fA-F]{2,})\s+(?:\(Inline(?: Function)?\)\s+-+`?-+\s+-+`?-+|(?P<sp>[0-9a-fA-F`]+)\s+"
    r"(?P<ret>[0-9a-fA-F`]+))\s+(?P<site>\S.*)$")
_CDB_MODULE_NAME_RE = re.compile(r"[\w.$~-]+")
_CDB_SOURCE_SUFFIX_RE = re.compile(r" @ (?P<line>\d+)\]$")
_CDB_OFFSET_SUFFIX_RE = re.compile(r"\+0x[0-9a-fA-F]+$")
_CDB_MODOFF_RE = re.compile(r"^(?P<module>[\w.$~-]+)\+(?P<off>0x[0-9a-fA-F]+)$")
_CDB_THREAD_RE = re.compile(
    r"^(?P<mark>[.#])?\s*(?P<num>\d+)\s+Id:\s*(?P<pid>[0-9a-fA-F]+)\.(?P<tid>[0-9a-fA-F]+)\s+Suspend:(?P<tail>.*)$")
_CDB_MODULE_RE = re.compile(r"^(?P<start>[0-9a-fA-F`]{8,})\s+(?P<end>[0-9a-fA-F`]{8,})\s+(?P<name>[\w.$~-]+)\s*(?P<rest>.*)$")
_CDB_NOT_LOADED_RE = re.compile(
    r"(?:symbols could not be loaded for|Defaulted to export symbols for|Unable to load image)\s+(?P<name>\S+)")
_CDB_MISMATCH_RE = re.compile(r"mismatched|WRONG_SYMBOLS|does not match", re.IGNORECASE)
_CDB_IMAGE_TOKEN_RE = re.compile(r"[\w.-]+")
_CDB_IMAGE_SUFFIXES = (".dll", ".exe", ".sys")
_CDB_EXC_RE = re.compile(r"ExceptionCode:\s+(?P<code>[0-9a-fA-F]{8})")
_CDB_EXC_ADDR_RE = re.compile(r"ExceptionAddress:\s+(?P<addr>[0-9a-fA-F`]+)")
_CDB_ATTEMPT_RE = re.compile(r"Attempt to (?P<kind>read|write|execute) (?:from|to) address (?P<addr>[0-9a-fA-F`]+)")
_CDB_PARAM_RE = re.compile(r"Parameter\[(?P<i>[01])\]:\s+(?P<value>[0-9a-fA-F`]+)")
_CDB_KV_RE = re.compile(r"^(?P<key>FAILURE_BUCKET_ID|SYMBOL_NAME|MODULE_NAME|IMAGE_NAME|PROCESS_NAME"
                        r"|FAILURE_ID_HASH|EXCEPTION_CODE_STR):\s+(?P<value>\S.*)$")


def _cdb_site(site: str) -> tuple[str, str, str, int | None] | None:
    """``module!func[+0xoff][ [file @ line]]`` -> (module, func, file, line).

    Procedural rather than one lazy regex: ``.+?`` followed by optional
    suffix groups retries every " [" of a hostile frame line (quadratic).
    """
    bang = site.find("!")
    if bang <= 0 or _CDB_MODULE_NAME_RE.fullmatch(site, 0, bang) is None:
        return None
    rest = site[bang + 1:]
    head, file_, line_no = rest, "", None
    source = _CDB_SOURCE_SUFFIX_RE.search(rest) if rest.endswith("]") else None
    if source is not None:
        start = rest.find(" [", 1)
        if 0 < start and start + 2 < source.start():
            head, file_, line_no = rest[:start], rest[start + 2:source.start()], int(source.group("line"))
    offset = _CDB_OFFSET_SUFFIX_RE.search(head)
    if offset is not None and offset.start() > 0:
        head = head[:offset.start()]
    if not head:
        return None
    return site[:bang], head, file_, line_no


def _cdb_thread_name(tail: str) -> str:
    """The last double-quoted string ending a ``~*`` thread header, if any."""
    text = tail.rstrip()
    if not text.endswith('"'):
        return ""
    start = text.rfind('"', 0, len(text) - 1)
    return text[start + 1:-1] if start >= 0 else ""


def _cdb_frame(line: str) -> StackFrame | None:
    match = _CDB_FRAME_RE.match(line.strip())
    if match is None:
        return None
    site = match.group("site").strip()
    inline = match.group("sp") is None
    module = ""
    function = ""
    offset = None
    file_ = ""
    line_no = None
    parsed = _cdb_site(site)
    if parsed is not None:
        module, function, file_, line_no = parsed
    else:
        bare = _CDB_MODOFF_RE.match(site)
        if bare is not None:
            module, offset = bare.group("module"), int(bare.group("off"), 16)
    return StackFrame(index=int(match.group("n"), 16), module=module, module_offset=offset,
                      function=function, file=file_, line=line_no, inline=inline,
                      trust=FrameTrust.DEBUGGER.value)


def _cdb_mismatched_image(line: str) -> str:
    """The first ``*.dll|exe|sys`` token after a symbol-mismatch phrase, lower-cased.

    Linear on purpose: the phrase is found once and the rest of the line is
    tokenized, so a debugger line repeating "mismatched" cannot trigger
    regex backtracking over the 2 MB window.
    """
    found = _CDB_MISMATCH_RE.search(line)
    if found is None:
        return ""
    for token in _CDB_IMAGE_TOKEN_RE.finditer(line, found.end()):
        value = token.group(0).lower()
        for suffix in _CDB_IMAGE_SUFFIXES:
            index = value.find(suffix)
            if index > 0:
                return value[:index + len(suffix)]
    return ""


def parse_cdb(text: str, nonce: str) -> DebuggerFindings:
    sections, order = _marker_sections(text, nonce)
    budget = [MAX_FRAMES_TOTAL]
    crash_frames = _frames(sections.get("STACK", []), _cdb_frame, budget)
    threads: list[tuple[int, tuple[StackFrame, ...]]] = []
    names: list[tuple[int, str]] = []
    crashing = None
    event_thread = None
    current_tid = None
    current: list[str] = []

    def flush() -> None:
        if current_tid is not None and len(threads) < MAX_THREADS:
            threads.append((current_tid, tuple(_frames(current, _cdb_frame, budget))))

    for line in sections.get("THREADS", []):
        header = _CDB_THREAD_RE.match(line.strip())
        if header is not None:
            flush()
            current_tid = int(header.group("tid"), 16)
            current = []
            if header.group("mark") == ".":
                crashing = current_tid
            elif header.group("mark") == "#":
                event_thread = current_tid
            name = _cdb_thread_name(header.group("tail"))
            if name:
                names.append((current_tid, name))
            continue
        current.append(line)
    flush()
    if crashing is None:
        crashing = event_thread
    if crash_frames:
        if crashing is None:
            crashing = threads[0][0] if threads else 0
        threads = [(tid, frames) for tid, frames in threads if tid != crashing]
        threads.insert(0, (crashing, tuple(crash_frames)))
    status: dict[str, str] = {}
    for name in sections:
        for line in sections[name]:
            mismatched = _cdb_mismatched_image(line)
            if mismatched:
                status[mismatched] = "mismatch"
                continue
            match = _CDB_NOT_LOADED_RE.search(line)
            if match is not None:
                status.setdefault(module_basename(match.group("name")).lower(), "not_found")
    modules = []
    for line in sections.get("MODULES", []):
        match = _CDB_MODULE_RE.match(line.strip())
        if match is None or len(modules) >= MAX_MODULES:
            continue
        start = _hex(match.group("start")) or 0
        end = _hex(match.group("end")) or 0
        name = match.group("name")
        image = (match.group("rest").split() or [""])[0]
        symbols = status.get(image.lower()) or status.get(name.lower()) or "not_attempted"
        modules.append(ModuleInfo(name=image if "." in image else name, base=start,
                                  size=max(0, end - start), symbols=symbols))
    notes = []
    exception = None
    code = None
    address = None
    access = ""
    access_address = None
    params: dict[int, int] = {}
    for line in sections.get("ANALYZE", []):
        stripped = line.strip()
        match = _CDB_EXC_RE.search(stripped)
        if match is not None and code is None:
            code = int(match.group("code"), 16)
        match = _CDB_EXC_ADDR_RE.search(stripped)
        if match is not None and address is None:
            address = _hex(match.group("addr"))
        match = _CDB_ATTEMPT_RE.search(stripped)
        if match is not None and not access:
            access = match.group("kind")
            access_address = _hex(match.group("addr"))
        match = _CDB_PARAM_RE.search(stripped)
        if match is not None:
            params.setdefault(int(match.group("i")), _hex(match.group("value")) or 0)
        match = _CDB_KV_RE.match(stripped)
        if match is not None and len(notes) < 8:
            notes.append("%s: %s" % (match.group("key"), match.group("value")))
    if code is not None:
        if not access and code == 0xC0000005 and 0 in params:
            access = ACCESS_KINDS.get(params[0], "access")
            access_address = params.get(1)
        detail = fast_fail_name(params[0]) if code == 0xC0000409 and 0 in params else (
            "C++ exception" if code == 0xE06D7363 else "")
        exception = CrashException(code="0x%08X" % code, name=ntstatus_name(code), address=address,
                                   access=access, access_address=access_address, thread_id=crashing,
                                   detail=detail)
    return DebuggerFindings(frames_by_thread=tuple(threads), crashing_thread=crashing, exception=exception,
                            modules=tuple(modules), sections_seen=tuple(order), notes=tuple(notes),
                            thread_names=tuple(names))


# --------------------------------------------------------------------- eu-stack

_EU_TID_RE = re.compile(r"^TID (?P<tid>\d+):$")
_EU_FRAME_RE = re.compile(r"^#(?P<n>\d+)\s+(?P<addr>0x[0-9a-fA-F]+)(?P<adj>\s+-\s+1)?\s*(?P<rest>.*)$")
_EU_BUILD_RE = re.compile(r"^\[(?P<build>[0-9a-fA-F]*)\]@(?P<base>0x[0-9a-fA-F]+)\+(?P<off>0x[0-9a-fA-F]+)$")
_EU_SRC_RE = re.compile(r"^(?P<file>\S.*?):(?P<line>\d+)(?::(?P<col>\d+))?$")


def parse_eu_stack(text: str) -> DebuggerFindings:
    threads: list[tuple[int, list[StackFrame]]] = []
    modules: dict[str, ModuleInfo] = {}
    budget = MAX_FRAMES_TOTAL
    last: StackFrame | None = None
    for raw in _window(text):
        line = raw.rstrip()
        stripped = line.strip()
        tid = _EU_TID_RE.match(stripped)
        if tid is not None:
            if len(threads) >= MAX_THREADS:
                break
            threads.append((int(tid.group("tid")), []))
            last = None
            continue
        frame = _EU_FRAME_RE.match(stripped) if line.startswith("#") else None
        if frame is not None and threads and budget > 0:
            rest = frame.group("rest")
            function, sep, module = rest.rpartition(" - ")
            if not sep:
                function, module = rest, ""
            function = "" if function.strip() in ("", "??") else function.strip()
            last = StackFrame(index=int(frame.group("n")), address=_hex(frame.group("addr")),
                              module=module.strip(), function=function, trust=FrameTrust.DEBUGGER.value)
            if len(threads[-1][1]) < MAX_FRAMES_PER_THREAD:
                threads[-1][1].append(last)
                budget -= 1
            continue
        if last is None or not line.startswith((" ", "\t")):
            continue
        build = _EU_BUILD_RE.match(stripped)
        if build is not None:
            base = int(build.group("base"), 16)
            offset = int(build.group("off"), 16)
            last = _replace_frame(threads, last, module_offset=offset)
            key = last.module.lower()
            if last.module and key not in modules and len(modules) < MAX_MODULES:
                modules[key] = ModuleInfo(name=last.module, base=base, debug_id=build.group("build").lower(),
                                          symbols="loaded" if last.function else "not_found")
            continue
        source = _EU_SRC_RE.match(stripped)
        if source is not None:
            last = _replace_frame(threads, last, file=source.group("file"), line=int(source.group("line")),
                                  column=int(source.group("col")) if source.group("col") else None)
    frames = tuple((tid, tuple(items)) for tid, items in threads)
    crashing = frames[0][0] if frames else None
    return DebuggerFindings(frames_by_thread=frames, crashing_thread=crashing, modules=tuple(modules.values()),
                            sections_seen=("eu-stack",) if frames else ())


def _replace_frame(threads, frame: StackFrame, **changes) -> StackFrame:
    updated = replace(frame, **changes)
    items = threads[-1][1]
    if items and items[-1] is frame:
        items[-1] = updated
    return updated


# --------------------------------------------------------------------- minidump-stackwalk

_STACKWALK_TRUST = {"context": "context", "cfi": "cfi", "cfi_scan": "scan", "frame_pointer": "frame_pointer",
                    "scan": "scan", "prewalked": "cfi", "inline": "cfi", "none": "scan"}


def _json_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _json_str(value) -> str:
    return value if isinstance(value, str) else ""


def parse_stackwalk_json(text: str) -> DebuggerFindings:
    """rust minidump-stackwalk ``--json`` output."""
    try:
        data = loads_bounded(text, max_bytes=MAX_TEXT_CHARS * 4)
    except InvalidInput as exc:
        raise CaptureFormatError("PARSE_FAILED", str(exc)) from None
    if not isinstance(data, dict):
        raise CaptureFormatError("PARSE_FAILED", "stackwalk JSON is not an object")
    crash = data.get("crash_info") if isinstance(data.get("crash_info"), dict) else {}
    crashing_index = _json_int(crash.get("crashing_thread"))
    budget = MAX_FRAMES_TOTAL
    threads = []
    names = []
    raw_threads = data.get("threads") if isinstance(data.get("threads"), list) else []
    for index, thread in enumerate(raw_threads[:MAX_THREADS]):
        if not isinstance(thread, dict):
            continue
        tid = _json_int(thread.get("thread_id"))
        tid = index if tid is None else tid
        frames = []
        raw_frames = thread.get("frames") if isinstance(thread.get("frames"), list) else []
        for frame in raw_frames[:MAX_FRAMES_PER_THREAD]:
            if not isinstance(frame, dict) or budget <= 0:
                break
            trust = _STACKWALK_TRUST.get(_json_str(frame.get("trust")), "scan")
            module = _json_str(frame.get("module"))
            inlines = frame.get("inlines") if isinstance(frame.get("inlines"), list) else []
            for inline in inlines[:16]:
                if isinstance(inline, dict):
                    frames.append(StackFrame(index=len(frames), module=module,
                                             function=_json_str(inline.get("function_name")),
                                             file=_json_str(inline.get("file")), line=_json_int(inline.get("line")),
                                             inline=True, trust=trust))
            frames.append(StackFrame(
                index=len(frames), address=_json_int(frame.get("offset")), module=module,
                module_offset=_json_int(frame.get("module_offset")), function=_json_str(frame.get("function")),
                file=_json_str(frame.get("file")), line=_json_int(frame.get("line")), trust=trust))
            budget -= 1
        threads.append((tid, tuple(frames)))
        if _json_str(thread.get("thread_name")):
            names.append((tid, _json_str(thread.get("thread_name"))))
    crashing = None
    if crashing_index is not None and 0 <= crashing_index < len(threads):
        crashing = threads[crashing_index][0]
        threads.insert(0, threads.pop(crashing_index))
    modules = []
    raw_modules = data.get("modules") if isinstance(data.get("modules"), list) else []
    for module in raw_modules[:MAX_MODULES]:
        if not isinstance(module, dict):
            continue
        base = _json_int(module.get("base_addr")) or 0
        end = _json_int(module.get("end_addr")) or base
        if module.get("corrupt_symbols"):
            symbols = "mismatch"
        elif module.get("loaded_symbols"):
            symbols = "loaded"
        elif module.get("missing_symbols"):
            symbols = "not_found"
        else:
            symbols = "not_attempted"
        modules.append(ModuleInfo(name=_json_str(module.get("filename")), base=base, size=max(0, end - base),
                                  version=_json_str(module.get("version")), debug_id=_json_str(module.get("debug_id")),
                                  debug_file=_json_str(module.get("debug_file")), symbols=symbols))
    exception = None
    if crash:
        exception = CrashException(name=_json_str(crash.get("type")),
                                   access_address=_json_int(crash.get("address")),
                                   detail=_json_str(crash.get("assertion")), thread_id=crashing)
    return DebuggerFindings(frames_by_thread=tuple(threads), crashing_thread=crashing, exception=exception,
                            modules=tuple(modules), sections_seen=("json",), thread_names=tuple(names))


def parse_stackwalk_machine(text: str) -> DebuggerFindings:
    """Breakpad C++ ``minidump_stackwalk -m`` pipe-delimited output."""
    threads: dict[int, list[StackFrame]] = {}
    modules = []
    crashing = None
    exception = None
    budget = MAX_FRAMES_TOTAL
    for raw in _window(text):
        parts = raw.rstrip("\r").split("|")
        if not parts or not parts[0]:
            continue
        head = parts[0]
        if head == "Crash" and len(parts) >= 4:
            crashing = _json_int(parts[3]) if parts[3].strip() else None
            exception = CrashException(name=parts[1], access_address=_json_int(parts[2]) if parts[2] else None,
                                       thread_id=crashing)
        elif head == "Module" and len(parts) >= 7 and len(modules) < MAX_MODULES:
            base = _json_int(parts[5]) or 0
            end = _json_int(parts[6]) or base
            modules.append(ModuleInfo(name=parts[1], version=parts[2], debug_file=parts[3], debug_id=parts[4],
                                      base=base, size=max(0, end - base)))
        elif head.isdigit() and len(parts) >= 7 and budget > 0:
            tid = int(head)
            if tid not in threads and len(threads) >= MAX_THREADS:
                continue
            frames = threads.setdefault(tid, [])
            if len(frames) >= MAX_FRAMES_PER_THREAD:
                continue
            offset = _json_int(parts[6]) if parts[6] else None
            function = parts[3]
            frames.append(StackFrame(
                index=_json_int(parts[1]) or len(frames), module=parts[2],
                module_offset=offset if not function and not parts[4] else None,
                function=function, file=parts[4], line=_json_int(parts[5]) if parts[5] else None,
                trust=FrameTrust.CFI.value))
            budget -= 1
    ordered = sorted(threads.items(), key=lambda item: (item[0] != crashing, item[0]))
    return DebuggerFindings(frames_by_thread=tuple((tid, tuple(f)) for tid, f in ordered), crashing_thread=crashing,
                            exception=exception, modules=tuple(modules), sections_seen=("machine",))


# --------------------------------------------------------------------- llvm-symbolizer

def parse_symbolizer_json(text: str) -> DebuggerFindings:
    """llvm-symbolizer ``--output-style=JSON``: a JSON array or one object per line."""
    stripped = text.strip() if isinstance(text, str) else ""
    records: list = []
    try:
        if stripped.startswith("["):
            value = loads_bounded(stripped, max_bytes=MAX_TEXT_CHARS * 4)
            records = value if isinstance(value, list) else []
        else:
            for line in stripped.splitlines()[:4096]:
                if line.strip():
                    records.append(loads_bounded(line, max_bytes=1 << 20))
    except InvalidInput as exc:
        raise CaptureFormatError("PARSE_FAILED", str(exc)) from None
    out = []
    for record in records[:4096]:
        if not isinstance(record, dict):
            continue
        offset = _json_int(record.get("Address"))
        if offset is None:
            continue
        module = module_basename(_json_str(record.get("ModuleName")))
        symbols = record.get("Symbol") if isinstance(record.get("Symbol"), list) else []
        frames = []
        for index, symbol in enumerate(symbols[:16]):
            if not isinstance(symbol, dict):
                continue
            frames.append(StackFrame(
                index=index, module=module, module_offset=offset,
                function=_json_str(symbol.get("FunctionName")), file=_json_str(symbol.get("FileName")),
                line=_json_int(symbol.get("Line")) or None, column=_json_int(symbol.get("Column")) or None,
                inline=index < len(symbols) - 1, trust=FrameTrust.SYMBOLIZER.value))
        out.append(SymbolizedAddress(module=module, offset=offset, frames=tuple(frames)))
    return DebuggerFindings(symbolized=tuple(out), sections_seen=("symbolizer",))


__all__ = [
    "DebuggerFindings", "SymbolizedAddress", "parse_cdb", "parse_eu_stack", "parse_gdb", "parse_lldb",
    "parse_stackwalk_json", "parse_stackwalk_machine", "parse_symbolizer_json",
]
