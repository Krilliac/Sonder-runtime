"""Pure ELF core reader over synthetic cores (x86_64, aarch64, PN_XNUM)."""
from __future__ import annotations

import struct

import pytest

from sonder_runtime.domain.binaries.reader import BytesReader
from sonder_runtime.domain.crash.elf_core import CoreLimits, core_to_report, read_elf_core
from sonder_runtime.domain.crash.model import CaptureFormatError


EXE_BASE = 0x5555_5555_4000
LIBC_BASE = 0x7FFF_F780_0000
STACK = 0x7FFF_FFFF_D000
BUILD_ID = bytes.fromhex("7f88edf63075c22be1e95606caab3d580e468a9c")


def _note(name: bytes, n_type: int, desc: bytes) -> bytes:
    name_z = name + b"\0"
    out = struct.pack("<III", len(name_z), len(desc), n_type) + name_z
    out += b"\0" * (-len(out) % 4) + desc
    return out + b"\0" * (-len(out) % 4)


def _module_image(build_id: bytes) -> bytes:
    """First page of a mapped ELF (ET_DYN) with a PT_NOTE build-id in memory."""
    page = bytearray(0x1000)
    page[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", page, 16, 3, 62, 1, 0x1040, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    struct.pack_into("<IIQQQQQQ", page, 64, 1, 5, 0, 0, 0, 0x1000, 0x1000, 0x1000)
    note = _note(b"GNU", 3, build_id)
    struct.pack_into("<IIQQQQQQ", page, 120, 4, 4, 0x200, 0x200, 0x200, len(note), len(note), 4)
    page[0x200:0x200 + len(note)] = note
    return bytes(page)


def _prstatus(machine: int, tid: int, pc: int, sp: int, fp: int, cursig: int) -> bytes:
    if machine == 62:
        regs = [0] * 27
        regs[4], regs[16], regs[19] = fp, pc, sp
        size = 336
    else:
        regs = [0] * 34
        regs[29], regs[31], regs[32] = fp, sp, pc
        size = 392
    desc = bytearray(size)
    struct.pack_into("<iii", desc, 0, cursig, 0, 0)
    struct.pack_into("<h", desc, 12, cursig)
    struct.pack_into("<i", desc, 32, tid)
    struct.pack_into("<%dQ" % len(regs), desc, 112, *regs)
    return bytes(desc)


def build_core(*, machine: int = 62, threads=None, signal: int = 11, si_code: int = 1, si_addr: int = 0,
               with_siginfo: bool = True, pn_xnum: bool = False, e_type: int = 4, fname: str = "crasher",
               extra_phdrs: int = 0) -> bytes:
    """A small ET_CORE with notes, an exe mapping (with build-id) and a stack."""
    if threads is None:
        fp = STACK + 0x100
        threads = [(4242, EXE_BASE + 0x1266, STACK + 0x40, fp), (4243, LIBC_BASE + 0x9000, STACK + 0x800, 0)]
    stack = bytearray(0x1000)
    # Frame-pointer chain: [fp] -> next fp, [fp+8] -> return address.
    struct.pack_into("<QQ", stack, 0x100, STACK + 0x180, EXE_BASE + 0x13A9)
    struct.pack_into("<QQ", stack, 0x180, 0, LIBC_BASE + 0x2A1CA)
    notes = b""
    for index, (tid, pc, sp, fp) in enumerate(threads):
        notes += _note(b"CORE", 1, _prstatus(machine, tid, pc, sp, fp, signal if index == 0 else 0))
        if index == 0:
            psinfo = bytearray(136)
            struct.pack_into("<i", psinfo, 24, tid)
            psinfo[40:40 + len(fname)] = fname.encode()
            psinfo[56:70] = b"crasher null\0\0"
            notes += _note(b"CORE", 3, bytes(psinfo))
            if with_siginfo:
                siginfo = bytearray(128)
                struct.pack_into("<iii", siginfo, 0, signal, 0, si_code)
                struct.pack_into("<Q", siginfo, 16, si_addr)
                notes += _note(b"CORE", 0x53494749, bytes(siginfo))
            notes += _note(b"CORE", 6, struct.pack("<QQQQ", 9, EXE_BASE + 0x1140, 0, 0))
            files = [(EXE_BASE, EXE_BASE + 0x1000, 0, b"/src/sparkmini/crasher"),
                     (EXE_BASE + 0x1000, EXE_BASE + 0x2000, 1, b"/src/sparkmini/crasher"),
                     (LIBC_BASE, LIBC_BASE + 0x200000, 0, b"/usr/lib/x86_64-linux-gnu/libc.so.6"),
                     (0x7FFF_F7A0_0000, 0x7FFF_F7A1_0000, 0, b"/usr/lib/locale/locale-archive")]
            blob = struct.pack("<QQ", len(files), 0x1000)
            blob += b"".join(struct.pack("<QQQ", s, e, o) for s, e, o, _ in files)
            blob += b"".join(path + b"\0" for *_, path in files)
            notes += _note(b"CORE", 0x46494C45, blob)
    image = _module_image(BUILD_ID)
    loads = [(EXE_BASE, image, 5), (EXE_BASE + 0x1000, b"", 5), (LIBC_BASE, b"", 5), (STACK, bytes(stack), 6)]
    phnum = 1 + len(loads) + extra_phdrs
    header = bytearray(64)
    header[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    phoff = 64
    shoff = 0
    data_start = phoff + phnum * 56
    if pn_xnum:
        shoff = data_start
        data_start += 64
    struct.pack_into("<HHIQQQIHHHHHH", header, 16, e_type, machine, 1, 0, phoff, shoff, 0, 64, 56,
                     0xFFFF if pn_xnum else phnum, 64, 1 if pn_xnum else 0, 0)
    body = bytearray()
    phdrs = bytearray()
    offset = data_start
    phdrs += struct.pack("<IIQQQQQQ", 4, 0, offset, 0, 0, len(notes), 0, 4)
    body += notes
    offset += len(notes)
    for vaddr, blob, flags in loads:
        phdrs += struct.pack("<IIQQQQQQ", 1, flags, offset, vaddr, 0, len(blob), max(len(blob), 0x1000), 0x1000)
        body += blob
        offset += len(blob)
    for _ in range(extra_phdrs):
        phdrs += struct.pack("<IIQQQQQQ", 0, 0, 0, 0, 0, 0, 0, 0)
    section0 = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, phnum, 0, 0) if pn_xnum else b""
    return bytes(header) + bytes(phdrs) + section0 + bytes(body)


def _report(data: bytes, **kwargs):
    return core_to_report(read_elf_core(BytesReader(data), **kwargs), source_label="core")


def _code(data: bytes, limits=None, **kwargs) -> str:
    with pytest.raises(CaptureFormatError) as info:
        read_elf_core(BytesReader(data), limits, **kwargs)
    return info.value.code


def test_x86_64_core_signal_modules_and_frames():
    report = _report(build_core())
    exc = report.exception
    assert exc.name == "SIGSEGV" and exc.detail == "SEGV_MAPERR" and exc.access_address == 0
    assert report.pid == 4242 and report.process_name == "crasher" and report.cpu == "x86_64"
    crashing = report.crashing_thread()
    assert crashing.thread_id == 4242
    top = crashing.frames[0]
    assert (top.module, top.module_offset, top.trust) == ("crasher", 0x1266, "context")
    assert [(f.module, f.module_offset, f.trust) for f in crashing.frames[1:]] == [
        ("crasher", 0x13A9, "frame_pointer"), ("libc.so.6", 0x2A1CA, "frame_pointer")]
    names = [module.name for module in report.modules]
    assert names[0] == "crasher" and "libc.so.6" in names and "locale-archive" not in names
    assert report.modules[0].debug_id == BUILD_ID.hex() and report.modules[0].in_project
    assert "null_deref" in {hint.kind for hint in report.hints}
    assert report.threads_total == 2 and len(report.threads) == 2


def test_aarch64_core():
    report = _report(build_core(machine=183))
    assert report.cpu == "aarch64"
    assert report.crashing_thread().frames[0].module_offset == 0x1266


def test_pn_xnum_core_reads_real_phdr_count():
    report = _report(build_core(pn_xnum=True))
    assert report.exception.name == "SIGSEGV"
    assert report.modules[0].debug_id == BUILD_ID.hex()


def test_core_without_signal_is_a_hang():
    report = _report(build_core(signal=0, with_siginfo=False))
    assert report.exception is None
    assert "hang_or_deadlock" in {hint.kind for hint in report.hints}


def test_sigabrt_without_fault_address():
    report = _report(build_core(signal=6, si_code=-6, si_addr=0x1234))
    assert report.exception.name == "SIGABRT" and report.exception.access_address is None
    assert "abort" in {hint.kind for hint in report.hints}


def test_not_core_and_unsupported_arch():
    assert _code(build_core(e_type=2)) == "NOT_CORE"
    assert _code(b"MZ" + b"\0" * 100) == "NOT_CORE"
    assert _code(build_core(machine=40)) == "UNSUPPORTED_ARCH"


def test_truncated_core():
    data = build_core()
    assert _code(data[:200]) == "TRUNCATED"


def test_phdr_limit():
    assert _code(build_core(extra_phdrs=10), CoreLimits(max_phdrs=8)) == "LIMIT_EXCEEDED"


def test_read_budget_and_time_budget():
    assert _code(build_core(), CoreLimits(max_bytes_read=500)) == "LIMIT_EXCEEDED"
    ticks = iter(range(0, 10_000_000, 3))
    assert _code(build_core(), CoreLimits(max_seconds=2.0), clock=lambda: next(ticks)) == "TIME_EXCEEDED"


def test_hostile_thread_count_does_not_explode():
    threads = [(1000 + i, EXE_BASE + 0x1266, STACK + 0x40, 0) for i in range(40)]
    report = _report(build_core(threads=threads))
    assert report.threads_total == 40 and len(report.threads) == 16 and report.truncated
