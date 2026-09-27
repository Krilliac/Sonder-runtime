"""Pure minidump reader (Windows MiniDumpWriteDump, Breakpad, Crashpad).

Streams read: ThreadList (3), ModuleList (4), MemoryList (5), Exception (6),
SystemInfo (7), Memory64List (9), UnloadedModuleList (14), MiscInfo (15),
ThreadNames (24), Breakpad info (0x47670001) and the Crashpad info stream
(0x43500001) with its simple annotations.

Hostile-input rules (SEC-008): every read is range-checked through a
``BudgetedReader``; entry counts are capped by both ``MinidumpLimits`` and
``stream_size // entry_size``; strings are UTF-16 with ``errors="replace"``
and at most ``max_string_chars``; loops check a wall-clock budget; nothing
recurses over input structures. Failures raise ``CaptureFormatError``.

Frames: frame 0 of each thread comes from its context (the exception
context for the crashing thread); further frames come from ``scan_stack``
(pointer-sized stack words that land inside a loaded module, trust=scan).
Debugger engines replace these frames later; module identity always comes
from here.
"""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from ..binaries.pe_debug import parse_rsds
from ..binaries.reader import (
    BinaryFormatError, BudgetedReader, ByteRangeError, ByteReader, WallClock, c_string,
)
from ..binaries.symstore import breakpad_debug_id
from ..diagnostics.model import clean_text
from .exceptions import (
    ACCESS_KINDS, FAULT_ADDRESS_SIGNALS, fast_fail_name, ntstatus_name, si_code_name, signal_name,
)
from .hints import cap_threads, finalize_report, is_system_module, order_modules
from .model import (
    MAX_ANNOTATIONS, MAX_CRASHING_FRAMES, MAX_MODULES, MAX_OTHER_FRAMES, MAX_OTHER_THREADS,
    Annotation, CaptureFormatError, CrashException, CrashReport, FrameTrust, ModuleInfo,
    StackFrame, ThreadSummary, module_basename,
)


MDMP_SIGNATURE = b"MDMP"
THREAD_LIST = 3
MODULE_LIST = 4
MEMORY_LIST = 5
EXCEPTION = 6
SYSTEM_INFO = 7
MEMORY64_LIST = 9
UNLOADED_MODULE_LIST = 14
MISC_INFO = 15
THREAD_NAMES = 24
BREAKPAD_INFO = 0x47670001
CRASHPAD_INFO = 0x43500001

ARCH_X86 = 0
ARCH_ARM = 5
ARCH_AMD64 = 9
ARCH_ARM64 = 12
ARCH_ARM64_BREAKPAD_OLD = 0x8003
ARCHES = {ARCH_X86: "x86", ARCH_AMD64: "amd64", ARCH_ARM64: "arm64", ARCH_ARM64_BREAKPAD_OLD: "arm64"}
# (pc offset, sp offset, pointer size) inside the thread CONTEXT record.
CONTEXT_OFFSETS = {"amd64": (0xF8, 0x98, 8), "arm64": (0x108, 0x100, 8), "x86": (0xB8, 0xC4, 4)}
PLATFORMS = {
    2: "Windows NT", 1: "Windows 9x", 0x8000: "Unix", 0x8101: "Mac OS X", 0x8102: "iOS",
    0x8201: "Linux", 0x8202: "Solaris", 0x8203: "Android", 0x8204: "PS3", 0x8205: "NaCl",
    0x8206: "Fuchsia",
}
_POSIX_SIGNAL_PLATFORMS = {0x8201, 0x8202, 0x8203, 0x8205, 0x8206, 0x8000}
_MACH_EXCEPTIONS = {1: "EXC_BAD_ACCESS", 2: "EXC_BAD_INSTRUCTION", 3: "EXC_ARITHMETIC",
                    4: "EXC_EMULATION", 5: "EXC_SOFTWARE", 6: "EXC_BREAKPOINT",
                    10: "EXC_CRASH", 11: "EXC_RESOURCE", 12: "EXC_GUARD"}
MANAGED_RUNTIME_MODULES = frozenset({"clr.dll", "coreclr.dll", "mscorwks.dll"})
CV_RSDS = b"RSDS"
CV_NB10 = b"NB10"
CV_BPEL = b"BpEL"
MAX_CV_RECORD = 24 + 260 * 4


@dataclass(frozen=True, slots=True)
class MinidumpLimits:
    max_streams: int = 128
    max_modules: int = 4096
    max_threads: int = 4096
    max_memory_ranges: int = 65536
    max_string_chars: int = 1024
    max_bytes_read: int = 32 << 20
    max_stack_scan_bytes: int = 256 << 10
    max_scan_frames: int = 32
    max_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class MemoryRange:
    start: int
    size: int
    rva: int


@dataclass(frozen=True, slots=True)
class RawThread:
    thread_id: int
    stack_start: int
    stack_size: int
    stack_rva: int
    context_size: int
    context_rva: int
    teb: int


@dataclass(frozen=True, slots=True)
class MinidumpTriage:
    flavor: str                              # windows | breakpad | crashpad
    arch: str
    os: str
    os_version: str
    process_id: int | None
    captured_at: int | None
    exception: CrashException | None
    crashing_thread_id: int | None
    threads: tuple[ThreadSummary, ...]
    threads_total: int
    modules: tuple[ModuleInfo, ...]
    modules_total: int
    annotations: tuple[Annotation, ...]
    notes: tuple[str, ...]
    truncated: bool
    managed_runtime: bool
    bytes_read: int = 0
    stream_types: tuple[int, ...] = ()

    @property
    def source_kind(self) -> str:
        return {"breakpad": "breakpad_minidump", "crashpad": "crashpad_minidump"}.get(
            self.flavor, "windows_minidump")


def _fail(code: str, detail: str) -> CaptureFormatError:
    return CaptureFormatError(code, detail)


class _Dump:
    def __init__(self, reader: ByteReader, limits: MinidumpLimits,
                 clock: Callable[[], float] | None) -> None:
        self.limits = limits
        self.reader = BudgetedReader(reader, limits.max_bytes_read)
        self.clock = WallClock(limits.max_seconds, clock)
        self.size = self.reader.size
        self.notes: list[str] = []
        self.truncated = False

    def read(self, offset: int, length: int, what: str) -> bytes:
        try:
            return self.reader.read(offset, length)
        except ByteRangeError:
            raise _fail("OUT_OF_BOUNDS", "%s at %d+%d is outside the file" % (what, offset, length)) from None

    def unpack(self, offset: int, fmt: str, what: str) -> tuple:
        return struct.unpack(fmt, self.read(offset, struct.calcsize(fmt), what))

    def string(self, rva: int, what: str) -> str:
        """A MINIDUMP_STRING (u32 byte length + UTF-16LE)."""
        if rva == 0:
            return ""
        (length,) = self.unpack(rva, "<I", what)
        if length % 2:
            raise _fail("OUT_OF_BOUNDS", "%s has an odd UTF-16 byte length" % what)
        if length > self.limits.max_string_chars * 2:
            raise _fail("LIMIT_EXCEEDED", "%s is longer than %d characters" % (what, self.limits.max_string_chars))
        raw = self.read(rva + 4, length, what)
        return raw.decode("utf-16-le", errors="replace")

    def utf8_string(self, rva: int, what: str, limit: int = 240) -> str:
        """A Crashpad MinidumpUTF8String (u32 length + bytes)."""
        (length,) = self.unpack(rva, "<I", what)
        if length > self.limits.max_string_chars * 4:
            raise _fail("LIMIT_EXCEEDED", "%s is too long" % what)
        return clean_text(self.read(rva + 4, length, what).decode("utf-8", errors="replace"), limit)

    def counted(self, count: int, entry_size: int, data_size: int, limit: int, what: str,
                header: int) -> int:
        if count > limit:
            raise _fail("LIMIT_EXCEEDED", "%d %s exceed the limit of %d" % (count, what, limit))
        if header + count * entry_size > data_size:
            raise _fail("TRUNCATED", "%d %s do not fit in a %d-byte stream" % (count, what, data_size))
        return count


def _directory(dump: _Dump) -> dict[int, tuple[int, int]]:
    if dump.size < 32:
        raise _fail("NOT_MINIDUMP", "too small for a minidump header")
    header = dump.read(0, 32, "header")
    if header[:4] != MDMP_SIGNATURE:
        raise _fail("NOT_MINIDUMP", "missing MDMP signature")
    (_version, n_streams, dir_rva, _checksum, _stamp, _flags) = struct.unpack_from("<IIIIIQ", header, 4)
    if n_streams == 0:
        raise _fail("TRUNCATED", "the stream directory is empty")
    if n_streams > dump.limits.max_streams:
        raise _fail("LIMIT_EXCEEDED", "%d streams exceed the limit of %d" % (n_streams, dump.limits.max_streams))
    if dir_rva > dump.size or n_streams * 12 > dump.size - dir_rva:
        raise _fail("TRUNCATED", "the stream directory extends past the end of the file")
    raw = dump.read(dir_rva, n_streams * 12, "stream directory")
    streams: dict[int, tuple[int, int]] = {}
    for index in range(n_streams):
        dump.clock.tick(64)
        stream_type, data_size, rva = struct.unpack_from("<III", raw, index * 12)
        if stream_type == 0 or data_size == 0:
            continue
        if rva > dump.size or data_size > dump.size - rva:
            raise _fail("TRUNCATED", "stream %d extends past the end of the file" % stream_type)
        if stream_type in streams:
            dump.notes.append("duplicate stream %d ignored" % stream_type)
            continue
        streams[stream_type] = (data_size, rva)
    return streams


def _version_string(fixed: bytes) -> str:
    signature, _struct_version, file_ms, file_ls = struct.unpack_from("<IIII", fixed, 0)
    if signature != 0xFEEF04BD:
        return ""
    return "%d.%d.%d.%d" % (file_ms >> 16, file_ms & 0xFFFF, file_ls >> 16, file_ls & 0xFFFF)


def _cv_identity(dump: _Dump, size: int, rva: int) -> tuple[str, str]:
    """(debug_id, debug_file) from a module's CodeView record."""
    if size < 4 or rva == 0:
        return "", ""
    raw = dump.read(rva, min(size, MAX_CV_RECORD), "CodeView record")
    if raw[:4] == CV_RSDS:
        parsed = parse_rsds(raw)
        if parsed is None:
            return "", ""
        guid, age, path = parsed
        return breakpad_debug_id(guid, age), module_basename(path)
    if raw[:4] == CV_NB10 and len(raw) >= 16:
        _offset, signature, age = struct.unpack_from("<III", raw, 4)
        name = c_string(raw[16:], 260).decode("utf-8", errors="replace")
        return "%08X%X" % (signature, age), module_basename(name)
    if raw[:4] == CV_BPEL:
        build_id = raw[4:4 + 64]
        return build_id.hex(), ""
    return "", ""


def _modules(dump: _Dump, streams, is_project) -> tuple[list[ModuleInfo], int, bool, bool]:
    entry = streams.get(MODULE_LIST)
    if entry is None:
        return [], 0, False, False
    data_size, rva = entry
    (count,) = dump.unpack(rva, "<I", "module list")
    count = dump.counted(count, 108, data_size, dump.limits.max_modules, "modules", 4)
    raw = dump.read(rva + 4, count * 108, "module list")
    modules: list[ModuleInfo] = []
    breakpad_cv = False
    managed = False
    for index in range(count):
        dump.clock.tick(64)
        off = index * 108
        base, size, _checksum, stamp, name_rva = struct.unpack_from("<QIIII", raw, off)
        version = _version_string(raw[off + 24:off + 76])
        cv_size, cv_rva = struct.unpack_from("<II", raw, off + 76)
        path = dump.string(name_rva, "module name")
        name = module_basename(path)
        cv_head = b""
        if cv_size >= 4 and cv_rva:
            cv_head = dump.read(cv_rva, 4, "CodeView record")
        breakpad_cv = breakpad_cv or cv_head == CV_BPEL
        debug_id, debug_file = _cv_identity(dump, cv_size, cv_rva)
        is_managed = name.lower() in MANAGED_RUNTIME_MODULES
        managed = managed or is_managed
        modules.append(ModuleInfo(
            name=name, path=path, base=base, size=size, version=version,
            timestamp=stamp or None, debug_id=debug_id, debug_file=debug_file,
            symbols="not_attempted", in_project=is_project(name, path),
            managed_runtime=is_managed,
        ))
    return modules, count, breakpad_cv, managed


def _memory(dump: _Dump, streams) -> list[MemoryRange]:
    ranges: list[MemoryRange] = []
    entry = streams.get(MEMORY_LIST)
    if entry is not None:
        data_size, rva = entry
        (count,) = dump.unpack(rva, "<I", "memory list")
        count = dump.counted(count, 16, data_size, dump.limits.max_memory_ranges, "memory ranges", 4)
        raw = dump.read(rva + 4, count * 16, "memory list")
        for index in range(count):
            dump.clock.tick(256)
            start, size, data_rva = struct.unpack_from("<QII", raw, index * 16)
            if data_rva > dump.size or size > dump.size - data_rva:
                raise _fail("OUT_OF_BOUNDS", "memory range data is outside the file")
            if size:
                ranges.append(MemoryRange(start, size, data_rva))
    entry = streams.get(MEMORY64_LIST)
    if entry is not None:
        data_size, rva = entry
        count, base_rva = dump.unpack(rva, "<QQ", "memory64 list")
        count = dump.counted(count, 16, data_size, dump.limits.max_memory_ranges, "memory64 ranges", 16)
        raw = dump.read(rva + 16, count * 16, "memory64 list")
        cursor = base_rva
        for index in range(count):
            dump.clock.tick(256)
            start, size = struct.unpack_from("<QQ", raw, index * 16)
            if cursor > dump.size or size > dump.size - cursor:
                raise _fail("OUT_OF_BOUNDS", "memory64 data (BaseRva + sizes) is outside the file")
            if size:
                ranges.append(MemoryRange(start, size, cursor))
            cursor += size
    ranges.sort(key=lambda item: item.start)
    for before, after in zip(ranges, ranges[1:]):
        if before.start + before.size > after.start:
            raise _fail("OUT_OF_BOUNDS", "memory ranges overlap at 0x%x" % after.start)
    return ranges


class _MemoryIndex:
    def __init__(self, ranges: list[MemoryRange]) -> None:
        self.ranges = ranges
        self.starts = [item.start for item in ranges]

    def find(self, address: int) -> MemoryRange | None:
        index = bisect.bisect_right(self.starts, address) - 1
        if index < 0:
            return None
        item = self.ranges[index]
        return item if item.start <= address < item.start + item.size else None


class ModuleIndex:
    def __init__(self, modules: Iterable[ModuleInfo]) -> None:
        self.modules = sorted((m for m in modules if m.size > 0), key=lambda m: m.base)
        self.bases = [m.base for m in self.modules]

    def find(self, address: int | None) -> ModuleInfo | None:
        if address is None:
            return None
        index = bisect.bisect_right(self.bases, address) - 1
        if index < 0:
            return None
        module = self.modules[index]
        return module if module.contains(address) else None


def frame_at(index: int, address: int | None, modules: ModuleIndex, trust: str) -> StackFrame:
    module = modules.find(address)
    return StackFrame(
        index=index, address=address,
        module=module.name if module else "",
        module_offset=(address - module.base) if module and address is not None else None,
        trust=trust, in_project=bool(module and module.in_project),
    )


def scan_stack(stack: bytes, stack_base: int, sp: int, modules, *, pointer_size: int,
               max_frames: int, start_index: int = 1, clock: WallClock | None = None) -> tuple[StackFrame, ...]:
    """Frames from stack words that point into a loaded module (trust=scan).

    ``stack`` holds memory starting at ``stack_base``; scanning starts at
    ``sp`` (or the start when ``sp`` is outside) and stops after
    ``max_frames`` hits. ``modules`` is a ``ModuleIndex`` or a sequence of
    ``ModuleInfo``.
    """
    index = modules if isinstance(modules, ModuleIndex) else ModuleIndex(modules)
    if not index.modules or max_frames <= 0:
        return ()
    fmt = "<Q" if pointer_size == 8 else "<I"
    offset = sp - stack_base if stack_base <= sp < stack_base + len(stack) else 0
    offset -= offset % pointer_size
    usable = (len(stack) - offset) // pointer_size * pointer_size
    # Cheap range pre-filter: most stack words are not code pointers, so only
    # words inside [lowest base, highest end) pay for the bisect lookup.
    low = index.bases[0]
    high = max(module.base + module.size for module in index.modules)
    frames: list[StackFrame] = []
    number = start_index
    for count, (value,) in enumerate(struct.iter_unpack(fmt, stack[offset:offset + usable]), 1):
        if clock is not None and not count & 4095:
            clock.check()
        if low <= value < high and index.find(value) is not None:
            frames.append(frame_at(number, value, index, FrameTrust.SCAN.value))
            number += 1
            if len(frames) >= max_frames:
                break
    return tuple(frames)


def _context_pc_sp(dump: _Dump, arch: str, size: int, rva: int) -> tuple[int | None, int | None]:
    layout = CONTEXT_OFFSETS.get(arch)
    if layout is None or rva == 0:
        return None, None
    pc_off, sp_off, width = layout
    need = max(pc_off, sp_off) + width
    if size < need:
        dump.notes.append("thread context too small for %s" % arch)
        return None, None
    fmt = "<Q" if width == 8 else "<I"
    raw = dump.read(rva, need, "thread context")
    return struct.unpack_from(fmt, raw, pc_off)[0], struct.unpack_from(fmt, raw, sp_off)[0]


def _threads(dump: _Dump, streams) -> tuple[list[RawThread], int]:
    entry = streams.get(THREAD_LIST)
    if entry is None:
        return [], 0
    data_size, rva = entry
    (count,) = dump.unpack(rva, "<I", "thread list")
    count = dump.counted(count, 48, data_size, dump.limits.max_threads, "threads", 4)
    raw = dump.read(rva + 4, count * 48, "thread list")
    threads = []
    for index in range(count):
        dump.clock.tick(256)
        (tid, _suspend, _pclass, _prio, teb, stack_start, stack_size, stack_rva, ctx_size,
         ctx_rva) = struct.unpack_from("<IIIIQQIIII", raw, index * 48)
        threads.append(RawThread(tid, stack_start, stack_size, stack_rva, ctx_size, ctx_rva, teb))
    return threads, count


def _thread_names(dump: _Dump, streams) -> dict[int, str]:
    entry = streams.get(THREAD_NAMES)
    if entry is None:
        return {}
    data_size, rva = entry
    (count,) = dump.unpack(rva, "<I", "thread names")
    count = dump.counted(count, 12, data_size, dump.limits.max_threads, "thread names", 4)
    raw = dump.read(rva + 4, count * 12, "thread names")
    names = {}
    for index in range(count):
        dump.clock.tick(256)
        tid, name_rva = struct.unpack_from("<IQ", raw, index * 12)
        if name_rva > dump.size:
            raise _fail("OUT_OF_BOUNDS", "thread name RVA is outside the file")
        names[tid] = dump.string(name_rva, "thread name")
    return names


def _system_info(dump: _Dump, streams) -> tuple[str, str, str, int]:
    entry = streams.get(SYSTEM_INFO)
    if entry is None:
        dump.notes.append("no system info stream; architecture unknown")
        return "unknown", "", "", -1
    data_size, rva = entry
    if data_size < 32:
        raise _fail("TRUNCATED", "system info stream is too small")
    (arch_code, _level, _revision, _nproc, _product, major, minor, build,
     platform, csd_rva) = dump.unpack(rva, "<HHHBBIIIII", "system info")
    arch = ARCHES.get(arch_code)
    if arch is None:
        raise _fail("UNSUPPORTED_ARCH", "processor architecture %d is not supported" % arch_code)
    os_name = PLATFORMS.get(platform, "platform 0x%x" % platform)
    version = "%d.%d.%d" % (major, minor, build)
    if csd_rva and csd_rva < dump.size:
        extra = clean_text(dump.string(csd_rva, "CSD version"), 120)
        if extra:
            version = "%s %s" % (version, extra)
    return arch, os_name, version, platform


def _misc(dump: _Dump, streams) -> tuple[int | None, int | None]:
    entry = streams.get(MISC_INFO)
    if entry is None:
        return None, None
    data_size, rva = entry
    if data_size < 16:
        return None, None
    (_size, flags, pid, create_time) = dump.unpack(rva, "<IIII", "misc info")
    return (pid if flags & 1 else None), (create_time if flags & 2 else None)


def _exception(dump: _Dump, streams, platform: int) -> tuple[CrashException | None, int | None, tuple[int, int]]:
    entry = streams.get(EXCEPTION)
    if entry is None:
        return None, None, (0, 0)
    data_size, rva = entry
    if data_size < 168:
        raise _fail("TRUNCATED", "exception stream is too small")
    raw = dump.read(rva, 168, "exception stream")
    tid, _align, code, flags, _record, address, n_params, _unused = struct.unpack_from("<IIIIQQII", raw, 0)
    params = struct.unpack_from("<15Q", raw, 40)[:min(n_params, 15)]
    ctx_size, ctx_rva = struct.unpack_from("<II", raw, 160)
    access = ""
    access_address = None
    detail = ""
    signal = ""
    if platform in _POSIX_SIGNAL_PLATFORMS:
        signal = signal_name(code)
        name = signal
        detail = si_code_name(signal, flags)
        if signal in FAULT_ADDRESS_SIGNALS:
            access_address = address
    elif platform in (0x8101, 0x8102):
        name = _MACH_EXCEPTIONS.get(code, "EXC_%d" % code)
        if code == 1:
            access_address = address
    else:
        name = ntstatus_name(code)
        if code in (0xC0000005, 0xC0000006) and len(params) >= 2:
            access = ACCESS_KINDS.get(params[0], "access")
            access_address = params[1]
        elif code == 0xC0000409 and params:
            detail = fast_fail_name(params[0])
        elif code == 0xE06D7363:
            detail = "C++ exception"
    exception = CrashException(
        code="0x%08X" % code, name=name, signal=signal, address=address, access=access,
        access_address=access_address, thread_id=tid, detail=detail,
    )
    return exception, tid, (ctx_size, ctx_rva)


def _crashpad_annotations(dump: _Dump, streams) -> tuple[list[Annotation], bool]:
    entry = streams.get(CRASHPAD_INFO)
    if entry is None:
        return [], False
    data_size, rva = entry
    if data_size < 52:
        dump.notes.append("crashpad info stream too small")
        return [], True
    (_version,) = dump.unpack(rva, "<I", "crashpad info")
    dict_size, dict_rva = dump.unpack(rva + 36, "<II", "crashpad annotations")
    if dict_size < 4 or dict_rva == 0:
        return [], True
    (count,) = dump.unpack(dict_rva, "<I", "crashpad annotations")
    if 4 + count * 8 > dict_size:
        raise _fail("TRUNCATED", "crashpad annotation count exceeds its dictionary")
    annotations = []
    for index in range(min(count, MAX_ANNOTATIONS)):
        dump.clock.tick(16)
        key_rva, value_rva = dump.unpack(dict_rva + 4 + index * 8, "<II", "crashpad annotation")
        annotations.append(Annotation(dump.utf8_string(key_rva, "annotation key"),
                                      dump.utf8_string(value_rva, "annotation value")))
    if count > MAX_ANNOTATIONS:
        dump.notes.append("%d crashpad annotations; first %d kept" % (count, MAX_ANNOTATIONS))
    return annotations, True


def _breakpad_requesting_thread(dump: _Dump, streams) -> int | None:
    entry = streams.get(BREAKPAD_INFO)
    if entry is None or entry[0] < 12:
        return None
    validity, _dump_tid, requesting = dump.unpack(entry[1], "<III", "breakpad info")
    return requesting if validity & 2 else None


def _unloaded(dump: _Dump, streams) -> list[ModuleInfo]:
    entry = streams.get(UNLOADED_MODULE_LIST)
    if entry is None:
        return []
    data_size, rva = entry
    if data_size < 12:
        return []
    header_size, entry_size, count = dump.unpack(rva, "<III", "unloaded modules")
    if header_size < 12 or entry_size < 24:
        raise _fail("OUT_OF_BOUNDS", "unloaded module list has an invalid layout")
    count = dump.counted(count, entry_size, data_size, dump.limits.max_modules, "unloaded modules", header_size)
    out = []
    for index in range(min(count, 64)):
        dump.clock.tick(64)
        base, size, _checksum, _stamp, name_rva = dump.unpack(
            rva + header_size + index * entry_size, "<QIIII", "unloaded module")
        path = dump.string(name_rva, "unloaded module name")
        out.append(ModuleInfo(name="<unloaded>" + module_basename(path), path=path, base=base, size=size))
    return out


def _default_is_project(name: str, path: str) -> bool:
    return bool(name) and not is_system_module(name, path)


def read_minidump(reader: ByteReader, limits: MinidumpLimits | None = None, *,
                  is_project_module: Callable[[str, str], bool] | None = None,
                  clock: Callable[[], float] | None = None) -> MinidumpTriage:
    """Parse a minidump into a ``MinidumpTriage``. Raises ``CaptureFormatError``."""
    limits = limits or MinidumpLimits()
    dump = _Dump(reader, limits, clock)
    is_project = is_project_module or _default_is_project
    try:
        return _read(dump, is_project)
    except CaptureFormatError:
        raise
    except BinaryFormatError as exc:
        raise _fail(exc.code, exc.detail) from None
    except (struct.error, OverflowError, MemoryError) as exc:
        raise _fail("OUT_OF_BOUNDS", type(exc).__name__) from None


def _read(dump: _Dump, is_project) -> MinidumpTriage:
    check = dump.clock.check
    streams = _directory(dump)
    check()
    arch, os_name, os_version, platform = _system_info(dump, streams)
    modules, modules_total, breakpad_cv, managed = _modules(dump, streams, is_project)
    check()
    unloaded = _unloaded(dump, streams)
    memory = _MemoryIndex(_memory(dump, streams))
    check()
    raw_threads, threads_total = _threads(dump, streams)
    names = _thread_names(dump, streams)
    check()
    exception, exc_tid, exc_ctx = _exception(dump, streams, platform)
    pid, created = _misc(dump, streams)
    annotations, crashpad = _crashpad_annotations(dump, streams)
    requesting = _breakpad_requesting_thread(dump, streams)
    check()
    breakpad = breakpad_cv or BREAKPAD_INFO in streams or platform in _POSIX_SIGNAL_PLATFORMS \
        or platform in (0x8101, 0x8102)
    flavor = "crashpad" if crashpad else ("breakpad" if breakpad else "windows")
    crashing_tid = exc_tid if exception is not None else None
    if crashing_tid is None and exception is not None:
        crashing_tid = requesting

    module_index = ModuleIndex(modules)
    unloaded_index = ModuleIndex(unloaded)
    pointer_size = CONTEXT_OFFSETS.get(arch, (0, 0, 8))[2]
    threads: list[ThreadSummary] = []
    crashing_ordered: list[ThreadSummary] = []
    for raw in raw_threads:
        dump.clock.tick(64)
        crashed = crashing_tid is not None and raw.thread_id == crashing_tid
        if not crashed and len(threads) >= MAX_OTHER_THREADS:
            # ``cap_threads`` keeps only the first MAX_OTHER_THREADS others;
            # reading and scanning the stacks of the rest would spend the
            # time and read budgets on threads that are dropped anyway.
            continue
        ctx_size, ctx_rva = (exc_ctx if crashed and exc_ctx[1] else (raw.context_size, raw.context_rva))
        pc, sp = _context_pc_sp(dump, arch, ctx_size, ctx_rva)
        frames: list[StackFrame] = []
        if pc is not None:
            frame = frame_at(0, pc, module_index, FrameTrust.CONTEXT.value)
            if not frame.module:
                gone = unloaded_index.find(pc)
                if gone is not None:
                    frame = replace(frame, module=gone.name, module_offset=pc - gone.base)
            frames.append(frame)
        max_frames = limits_scan(dump.limits, crashed)
        if sp is not None and max_frames:
            stack = _stack_bytes(dump, raw, sp, memory, crashed)
            if stack is not None:
                base, data = stack
                frames += scan_stack(data, base, sp, module_index, pointer_size=pointer_size,
                                     max_frames=max_frames, start_index=len(frames), clock=dump.clock)
        summary = ThreadSummary(thread_id=raw.thread_id, name=names.get(raw.thread_id, ""),
                                crashed=crashed, frames=tuple(frames))
        (crashing_ordered if crashed else threads).append(summary)
    if crashing_tid is not None and not crashing_ordered:
        dump.notes.append("exception thread %d is not in the thread list" % crashing_tid)
    ordered = cap_threads(crashing_ordered + threads, crashing_tid, max_crashing_frames=MAX_CRASHING_FRAMES,
                          max_others=MAX_OTHER_THREADS, max_other_frames=MAX_OTHER_FRAMES)
    if len(raw_threads) > len(ordered):
        dump.truncated = True
    crash_module = None
    if crashing_ordered and crashing_ordered[0].frames:
        crash_module = module_index.find(crashing_ordered[0].frames[0].address)
    elif exception is not None:
        crash_module = module_index.find(exception.address)
    if len(modules) > MAX_MODULES:
        dump.truncated = True
    if managed:
        dump.notes.append("managed runtime module present (clr/coreclr/mscorwks)")
    if unloaded:
        dump.notes.append("unloaded modules: %s" % ", ".join(m.name[10:] for m in unloaded[:8]))
    return MinidumpTriage(
        flavor=flavor, arch=arch, os=os_name, os_version=os_version, process_id=pid,
        captured_at=created, exception=exception, crashing_thread_id=crashing_tid,
        threads=ordered, threads_total=threads_total,
        modules=order_modules(modules, crash_module, MAX_MODULES), modules_total=modules_total,
        annotations=tuple(annotations), notes=tuple(dump.notes), truncated=dump.truncated,
        managed_runtime=managed, bytes_read=dump.reader.bytes_read,
        stream_types=tuple(sorted(streams)),
    )


def limits_scan(limits: MinidumpLimits, crashed: bool) -> int:
    if crashed:
        return max(0, min(limits.max_scan_frames, MAX_CRASHING_FRAMES - 1))
    return max(0, min(limits.max_scan_frames, MAX_OTHER_FRAMES - 1))


def _stack_bytes(dump: _Dump, raw: RawThread, sp: int, memory: _MemoryIndex,
                 crashed: bool) -> tuple[int, bytes] | None:
    budget = dump.limits.max_stack_scan_bytes if crashed else min(16 << 10, dump.limits.max_stack_scan_bytes)
    if raw.stack_rva and raw.stack_size:
        if raw.stack_rva > dump.size or raw.stack_size > dump.size - raw.stack_rva:
            raise _fail("OUT_OF_BOUNDS", "thread stack memory is outside the file")
        start = raw.stack_start
        offset = sp - start if start <= sp < start + raw.stack_size else 0
        length = min(raw.stack_size - offset, budget)
        return start + offset, dump.read(raw.stack_rva + offset, length, "thread stack")
    found = memory.find(sp)
    if found is None:
        return None
    offset = sp - found.start
    length = min(found.size - offset, budget)
    return sp, dump.read(found.rva + offset, length, "thread stack")


def triage_to_report(triage: MinidumpTriage, *, source_label: str = "", input_sha256: str = "",
                     input_bytes: int = 0) -> CrashReport:
    """A finalized Tier-0 ``CrashReport`` (hints, signature, symbolication)."""
    process_name = ""
    for module in triage.modules:
        if module.path.lower().endswith(".exe") or (module.in_project and not process_name):
            process_name = module.name
            if module.path.lower().endswith(".exe"):
                break
    report = CrashReport(
        source_kind=triage.source_kind, engines=("pure",), source_label=source_label,
        input_sha256=input_sha256, input_bytes=input_bytes,
        os=("%s %s" % (triage.os, triage.os_version)).strip(), cpu=triage.arch,
        process_name=process_name, pid=triage.process_id, captured_at=triage.captured_at,
        exception=triage.exception, crashing_thread_id=triage.crashing_thread_id,
        threads=triage.threads, threads_total=triage.threads_total, modules=triage.modules,
        modules_total=triage.modules_total, annotations=triage.annotations,
        notes=triage.notes, truncated=triage.truncated,
    )
    return finalize_report(report)


__all__ = [
    "CONTEXT_OFFSETS", "MANAGED_RUNTIME_MODULES", "MinidumpLimits", "MinidumpTriage",
    "read_minidump", "scan_stack", "triage_to_report",
]
