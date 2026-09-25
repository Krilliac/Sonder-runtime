"""Pure ELF core reader (x86_64, aarch64).

Notes read: NT_PRSTATUS (one per thread; the first is the thread that
received the fatal signal), NT_SIGINFO (signal, si_code, si_addr),
NT_PRPSINFO (process name and pid), NT_FILE (mapped files, which become
modules) and NT_AUXV (AT_ENTRY names the executable). Module build-ids are
read from the ELF headers that the kernel or gdb dumped into the core's
PT_LOAD memory, when present.

Bounds (SEC-008): ``CoreLimits`` caps program headers, notes, NT_FILE
entries, total bytes read and wall-clock time; every read is range-checked.
Failures raise ``CaptureFormatError``.
"""
from __future__ import annotations

import bisect
import re
import struct
from dataclasses import dataclass
from typing import Callable

from ..binaries.elf_ids import (
    ET_CORE, PT_LOAD, PT_NOTE, build_id_from_notes, iter_notes, program_headers, read_elf_header,
)
from ..binaries.reader import BinaryFormatError, BudgetedReader, ByteRangeError, ByteReader, WallClock, c_string
from ..diagnostics.model import clean_text
from .exceptions import FAULT_ADDRESS_SIGNALS, si_code_name, signal_name
from .hints import cap_threads, finalize_report, is_system_module, order_modules
from .minidump import ModuleIndex, frame_at, scan_stack
from .model import (
    MAX_CRASHING_FRAMES, MAX_MODULES, MAX_OTHER_FRAMES, MAX_OTHER_THREADS, CaptureFormatError,
    CrashException, CrashReport, FrameTrust, ModuleInfo, StackFrame, ThreadSummary, module_basename,
)


NT_PRSTATUS = 1
NT_PRPSINFO = 3
NT_AUXV = 6
NT_SIGINFO = 0x53494749
NT_FILE = 0x46494C45
AT_ENTRY = 9
EM_X86_64 = 62
EM_AARCH64 = 183
PF_X = 1
# (offset of pr_reg in elf_prstatus, pc index, sp index, fp index) per
# machine; pid at 32. x86_64 user_regs_struct: rbp=4, rip=16, rsp=19;
# aarch64 user_pt_regs: x29=29, sp=31, pc=32.
PRSTATUS_LAYOUT = {EM_X86_64: (112, 16, 19, 4), EM_AARCH64: (112, 32, 31, 29)}
ARCH_NAMES = {EM_X86_64: "x86_64", EM_AARCH64: "aarch64"}
MAX_MODULE_PROBES = 256
_SHARED_OBJECT_RE = re.compile(r"\.so(?:\.[0-9.]+)?$")


@dataclass(frozen=True, slots=True)
class CoreLimits:
    max_phdrs: int = 4096
    max_notes: int = 4096
    max_nt_file_entries: int = 8192
    max_bytes_read: int = 16 << 20
    max_seconds: float = 2.0
    max_stack_scan_bytes: int = 256 << 10
    max_scan_frames: int = 32


@dataclass(frozen=True, slots=True)
class CoreTriage:
    arch: str
    process_name: str
    pid: int | None
    exception: CrashException | None
    crashing_thread_id: int | None
    threads: tuple[ThreadSummary, ...]
    threads_total: int
    modules: tuple[ModuleInfo, ...]
    modules_total: int
    notes: tuple[str, ...]
    truncated: bool
    command_line: str = ""
    bytes_read: int = 0


@dataclass(frozen=True, slots=True)
class _Segment:
    vaddr: int
    filesz: int
    memsz: int
    offset: int
    flags: int


class _CoreMemory:
    """Address-space reads backed by PT_LOAD file contents."""

    def __init__(self, reader: ByteReader, segments: list[_Segment]) -> None:
        self.reader = reader
        self.segments = sorted((s for s in segments if s.filesz), key=lambda s: s.vaddr)
        self.starts = [s.vaddr for s in self.segments]

    def find(self, address: int) -> _Segment | None:
        index = bisect.bisect_right(self.starts, address) - 1
        if index < 0:
            return None
        segment = self.segments[index]
        return segment if segment.vaddr <= address < segment.vaddr + segment.filesz else None

    def read(self, address: int, length: int) -> bytes | None:
        segment = self.find(address)
        if segment is None or address + length > segment.vaddr + segment.filesz:
            return None
        try:
            return self.reader.read(segment.offset + (address - segment.vaddr), length)
        except ByteRangeError:
            return None

    def available(self, address: int, want: int) -> bytes | None:
        segment = self.find(address)
        if segment is None:
            return None
        length = min(want, segment.vaddr + segment.filesz - address)
        return self.read(address, length)


class _Window:
    """A ``ByteReader`` view of process memory starting at ``base``."""

    def __init__(self, memory: _CoreMemory, base: int, size: int) -> None:
        self._memory = memory
        self._base = base
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self._size:
            raise ByteRangeError("window read outside the module")
        data = self._memory.read(self._base + offset, length)
        if data is None:
            raise ByteRangeError("module bytes not present in the core")
        return data


def _fail(code: str, detail: str) -> CaptureFormatError:
    return CaptureFormatError(code, detail)


def _default_is_project(name: str, path: str) -> bool:
    return bool(name) and not is_system_module(name, path)


def read_elf_core(reader: ByteReader, limits: CoreLimits | None = None, *,
                  is_project_module: Callable[[str, str], bool] | None = None,
                  clock: Callable[[], float] | None = None) -> CoreTriage:
    """Parse an ELF core into a ``CoreTriage``. Raises ``CaptureFormatError``."""
    limits = limits or CoreLimits()
    budget = BudgetedReader(reader, limits.max_bytes_read)
    wall = WallClock(limits.max_seconds, clock)
    try:
        return _read(budget, limits, wall, is_project_module or _default_is_project)
    except CaptureFormatError:
        raise
    except BinaryFormatError as exc:
        code = {"NOT_ELF": "NOT_CORE"}.get(exc.code, exc.code)
        raise _fail(code, exc.detail) from None
    except (struct.error, OverflowError, MemoryError) as exc:
        raise _fail("OUT_OF_BOUNDS", type(exc).__name__) from None


def _read(reader: BudgetedReader, limits: CoreLimits, wall: WallClock, is_project) -> CoreTriage:
    header = read_elf_header(reader)
    if header.type != ET_CORE:
        raise _fail("NOT_CORE", "ELF file is not a core (e_type %d)" % header.type)
    layout = PRSTATUS_LAYOUT.get(header.machine)
    if layout is None or header.bits != 64 or header.endian != "<":
        raise _fail("UNSUPPORTED_ARCH", "core machine %d is not supported" % header.machine)
    phdrs = program_headers(reader, header, limit=limits.max_phdrs, clock=wall)
    segments = [_Segment(p.vaddr, p.filesz, p.memsz, p.offset, p.flags) for p in phdrs if p.type == PT_LOAD]
    notes_seen = 0
    prstatus: list[bytes] = []
    siginfo = b""
    prpsinfo = b""
    nt_file = b""
    auxv = b""
    report_notes: list[str] = []
    truncated = False
    for phdr in phdrs:
        if phdr.type != PT_NOTE or not phdr.filesz:
            continue
        try:
            blob = reader.read(phdr.offset, phdr.filesz)
        except ByteRangeError:
            raise _fail("TRUNCATED", "PT_NOTE segment extends past the end of the core") from None
        for name, n_type, desc in iter_notes(blob, header.endian, 4):
            wall.tick(128)
            notes_seen += 1
            if notes_seen > limits.max_notes:
                raise _fail("LIMIT_EXCEEDED", "more than %d notes" % limits.max_notes)
            if name not in (b"CORE", b"LINUX"):
                continue
            if n_type == NT_PRSTATUS:
                prstatus.append(desc)
            elif n_type == NT_SIGINFO and not siginfo:
                siginfo = desc
            elif n_type == NT_PRPSINFO and not prpsinfo:
                prpsinfo = desc
            elif n_type == NT_FILE and not nt_file:
                nt_file = desc
            elif n_type == NT_AUXV and not auxv:
                auxv = desc
    if not prstatus:
        raise _fail("TRUNCATED", "core has no NT_PRSTATUS note")

    wall.check()
    memory = _CoreMemory(reader, segments)
    exec_ranges = [(s.vaddr, s.vaddr + max(s.memsz, s.filesz)) for s in segments if s.flags & PF_X]
    entry = _auxv_entry(auxv)
    modules, modules_total = _modules(nt_file, limits, wall, memory, exec_ranges, is_project,
                                      report_notes, entry)
    process_name = ""
    pid = None
    command_line = ""
    if len(prpsinfo) >= 136:
        pid = struct.unpack_from("<i", prpsinfo, 24)[0]
        process_name = c_string(prpsinfo[40:56], 16).decode("utf-8", "replace")
        command_line = clean_text(c_string(prpsinfo[56:136], 80).decode("utf-8", "replace"), 80)
    exe = None
    if entry is not None:
        exe = next((m for m in modules if m.contains(entry)), None)
    if exe is None and process_name:
        exe = next((m for m in modules if m.name.startswith(process_name[:15])), None)
    if exe is not None:
        process_name = exe.name

    reg_off, pc_index, sp_index, fp_index = layout
    module_index = ModuleIndex(modules)
    threads: list[ThreadSummary] = []
    first_signal = 0
    for number, desc in enumerate(prstatus):
        wall.tick(64)
        if len(desc) < reg_off + (max(pc_index, sp_index) + 1) * 8:
            report_notes.append("short NT_PRSTATUS note skipped")
            continue
        cursig = struct.unpack_from("<h", desc, 12)[0]
        tid = struct.unpack_from("<i", desc, 32)[0]
        pc = struct.unpack_from("<Q", desc, reg_off + pc_index * 8)[0]
        sp = struct.unpack_from("<Q", desc, reg_off + sp_index * 8)[0]
        fp = struct.unpack_from("<Q", desc, reg_off + fp_index * 8)[0]
        if number == 0:
            first_signal = cursig
        crashed = number == 0
        frames: list[StackFrame] = [frame_at(0, pc, module_index, FrameTrust.CONTEXT.value)]
        max_frames = min(limits.max_scan_frames, (MAX_CRASHING_FRAMES if crashed else MAX_OTHER_FRAMES) - 1)
        walked = frame_pointer_walk(memory, sp, fp, module_index, max_frames=max_frames, clock=wall)
        if walked:
            frames += walked
        else:
            want = limits.max_stack_scan_bytes if crashed else min(16 << 10, limits.max_stack_scan_bytes)
            stack = memory.available(sp, want)
            if stack:
                frames += scan_stack(stack, sp, sp, module_index, pointer_size=8, max_frames=max_frames,
                                     start_index=1, clock=wall)
        threads.append(ThreadSummary(thread_id=tid, crashed=crashed, frames=tuple(frames)))

    wall.check()
    exception = _exception(siginfo, first_signal, threads[0] if threads else None)
    crashing_tid = threads[0].thread_id if threads and exception is not None else None
    if exception is None and threads:
        threads[0] = ThreadSummary(thread_id=threads[0].thread_id, name=threads[0].name,
                                   crashed=False, frames=threads[0].frames)
    ordered = cap_threads(threads, crashing_tid, max_crashing_frames=MAX_CRASHING_FRAMES,
                          max_others=MAX_OTHER_THREADS, max_other_frames=MAX_OTHER_FRAMES)
    if len(threads) > len(ordered):
        truncated = True
    crash_module = None
    if crashing_tid is not None and ordered and ordered[0].frames:
        crash_module = module_index.find(ordered[0].frames[0].address)
    if len(modules) > MAX_MODULES:
        truncated = True
    return CoreTriage(
        arch=ARCH_NAMES[header.machine], process_name=process_name, pid=pid, exception=exception,
        crashing_thread_id=crashing_tid, threads=ordered, threads_total=len(prstatus),
        modules=order_modules(modules, crash_module, MAX_MODULES), modules_total=modules_total,
        notes=tuple(report_notes), truncated=truncated, command_line=command_line,
        bytes_read=reader.bytes_read,
    )


def frame_pointer_walk(memory: _CoreMemory, sp: int, fp: int, modules: ModuleIndex, *,
                       max_frames: int, clock: WallClock | None = None) -> tuple[StackFrame, ...]:
    """Follow the saved frame-pointer chain (``[fp]`` = caller fp, ``[fp+8]`` = return).

    Stops when the chain leaves dumped memory, stops increasing, strays more
    than 8 MiB above ``sp`` or the return address is outside every module.
    """
    frames: list[StackFrame] = []
    previous = sp
    while fp and len(frames) < max_frames:
        if clock is not None:
            clock.tick(256)
        if fp < previous or fp - sp > (8 << 20) or fp % 8:
            break
        raw = memory.read(fp, 16)
        if raw is None:
            break
        next_fp, ret = struct.unpack("<QQ", raw)
        if not ret or modules.find(ret) is None:
            break
        # A return address points after the call; attribute it to the call.
        frames.append(frame_at(len(frames) + 1, ret, modules, FrameTrust.FRAME_POINTER.value))
        previous = fp + 16
        fp = next_fp
    return tuple(frames)


def _auxv_entry(auxv: bytes) -> int | None:
    for offset in range(0, min(len(auxv), 4096) - 15, 16):
        key, value = struct.unpack_from("<QQ", auxv, offset)
        if key == AT_ENTRY:
            return value
        if key == 0:
            break
    return None


def _exception(siginfo: bytes, cursig: int, thread: ThreadSummary | None) -> CrashException | None:
    signo = 0
    code = 0
    address = None
    if len(siginfo) >= 24:
        signo, _errno, code = struct.unpack_from("<iii", siginfo, 0)
        address = struct.unpack_from("<Q", siginfo, 16)[0]
    if not signo:
        signo = cursig
        address = None
    if not signo:
        return None
    name = signal_name(signo)
    pc = thread.frames[0].address if thread and thread.frames else None
    return CrashException(
        code=str(signo), name=name, signal=name, address=pc, access="",
        access_address=address if name in FAULT_ADDRESS_SIGNALS else None,
        thread_id=thread.thread_id if thread else None,
        detail=si_code_name(name, code) if siginfo else "",
    )


def _modules(nt_file: bytes, limits: CoreLimits, wall: WallClock, memory: _CoreMemory,
             exec_ranges, is_project, notes: list[str], entry: int | None) -> tuple[list[ModuleInfo], int]:
    if len(nt_file) < 16:
        return [], 0
    count, _page_size = struct.unpack_from("<QQ", nt_file, 0)
    if count > limits.max_nt_file_entries:
        raise _fail("LIMIT_EXCEEDED", "%d NT_FILE entries exceed the limit" % count)
    table_end = 16 + count * 24
    if table_end > len(nt_file):
        raise _fail("TRUNCATED", "NT_FILE entry table exceeds its note")
    names_blob = nt_file[table_end:]
    cursor = 0
    grouped: dict[str, list[tuple[int, int, int]]] = {}
    order: list[str] = []
    for index in range(count):
        wall.tick(256)
        start, end, file_ofs = struct.unpack_from("<QQQ", nt_file, 16 + index * 24)
        stop = names_blob.find(b"\x00", cursor)
        if stop < 0:
            raise _fail("TRUNCATED", "NT_FILE names are truncated")
        path = names_blob[cursor:stop].decode("utf-8", "replace")
        cursor = stop + 1
        if end < start:
            raise _fail("OUT_OF_BOUNDS", "NT_FILE mapping ends before it starts")
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append((start, end, file_ofs))
    exec_starts = sorted(exec_ranges)
    modules: list[ModuleInfo] = []
    for path in order:
        wall.tick(64)
        ranges = grouped[path]
        name = module_basename(path)
        executable = any(_overlaps(start, end, exec_starts) for start, end, _ in ranges)
        is_entry = entry is not None and any(start <= entry < end for start, end, _ in ranges)
        if exec_starts and not (executable or is_entry or _SHARED_OBJECT_RE.search(name)):
            continue
        zero = [start for start, _end, ofs in ranges if ofs == 0]
        base = min(zero) if zero else min(start for start, _e, _o in ranges)
        size = max(end for _s, end, _o in ranges) - base
        build_id = ""
        if len(modules) < MAX_MODULE_PROBES:
            build_id = _memory_build_id(memory, base, size)
        modules.append(ModuleInfo(name=name, path=path, base=base, size=size, debug_id=build_id,
                                  in_project=is_project(name, path)))
    if not modules and grouped:
        notes.append("no executable mappings matched NT_FILE entries")
    return modules, len(modules)


def _overlaps(start: int, end: int, ranges) -> bool:
    for low, high in ranges:
        if low < end and start < high:
            return True
    return False


def _memory_build_id(memory: _CoreMemory, base: int, size: int) -> str:
    window = _Window(memory, base, max(size, 0))
    try:
        header = read_elf_header(window)
        phdrs = program_headers(window, header, limit=128)
    except BinaryFormatError:
        return ""
    loads = [p.vaddr for p in phdrs if p.type == PT_LOAD]
    low = min(loads) & ~0xFFF if loads else 0
    for phdr in phdrs:
        if phdr.type != PT_NOTE or not 0 < phdr.filesz <= 4096:
            continue
        try:
            blob = window.read(phdr.vaddr - low, phdr.filesz)
        except BinaryFormatError:
            continue
        found = build_id_from_notes(blob, header.endian, phdr.align)
        if found:
            return found
    return ""


def core_to_report(triage: CoreTriage, *, source_label: str = "", input_sha256: str = "",
                   input_bytes: int = 0) -> CrashReport:
    report = CrashReport(
        source_kind="elf_core", engines=("pure",), source_label=source_label,
        input_sha256=input_sha256, input_bytes=input_bytes, os="Linux", cpu=triage.arch,
        process_name=triage.process_name, pid=triage.pid, exception=triage.exception,
        crashing_thread_id=triage.crashing_thread_id, threads=triage.threads,
        threads_total=triage.threads_total, modules=triage.modules,
        modules_total=triage.modules_total, notes=triage.notes, truncated=triage.truncated,
    )
    return finalize_report(report)


__all__ = ["CoreLimits", "CoreTriage", "core_to_report", "read_elf_core"]
