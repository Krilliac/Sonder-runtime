"""Pure minidump reader over synthetic MDMP files (tests/support/minidump_builder)."""
from __future__ import annotations

import struct

import pytest

from sonder_runtime.domain.binaries.reader import BytesReader
from sonder_runtime.domain.crash.minidump import MinidumpLimits, read_minidump, scan_stack, triage_to_report
from sonder_runtime.domain.crash.model import CaptureFormatError, ModuleInfo
from tests.support.minidump_builder import (
    AMD64, ARM64, GAME_BASE, LINUX, NTDLL_BASE, STACK_BASE, X86, MinidumpBuilder, amd64_access_violation,
    bpel_record, stack_with_returns,
)


def _report(data: bytes, **kwargs):
    return triage_to_report(read_minidump(BytesReader(data), **kwargs), source_label="x.dmp")


def _code(data: bytes, limits: MinidumpLimits | None = None, **kwargs) -> str:
    with pytest.raises(CaptureFormatError) as info:
        read_minidump(BytesReader(data), limits, **kwargs)
    return info.value.code


def _hints(report) -> set[str]:
    return {hint.kind for hint in report.hints}


# ------------------------------------------------------------------ happy paths

def test_amd64_access_violation_report():
    report = _report(amd64_access_violation())
    assert report.source_kind == "windows_minidump" and report.cpu == "amd64"
    assert report.os.startswith("Windows NT 10.0.19045")
    exc = report.exception
    assert exc.name == "EXCEPTION_ACCESS_VIOLATION" and exc.code == "0xC0000005"
    assert exc.access == "read" and exc.access_address == 0
    crashing = report.crashing_thread()
    assert crashing.thread_id == 0x1B3C and crashing.name == "MainThread"
    top = crashing.frames[0]
    assert (top.module, top.module_offset, top.trust) == ("spark_game.exe", 0x1234, "context")
    assert [f.trust for f in crashing.frames[1:]] == ["scan", "scan", "scan"]
    assert report.modules[0].name == "spark_game.exe"
    assert report.modules[0].debug_id == "1B4E28BA2FA111D2883FB9A761BDE3FB3"
    assert report.modules[0].debug_file == "spark_game.pdb" and report.modules[0].version == "1.2.3.4"
    assert report.modules[0].in_project and not report.module_named("ntdll.dll").in_project
    assert report.pid == 4242 and report.process_name == "spark_game.exe"
    assert "null_deref" in _hints(report)
    assert report.signature_basis == "module_offsets" and len(report.signature) == 16
    assert report.untrusted_strings is True


def test_write_av_near_fill_pattern_hints_use_after_free():
    report = _report(amd64_access_violation(read_address=0xDDDDDDDD + 8, write=True))
    assert report.exception.access == "write"
    assert "use_after_free" in _hints(report)


def _single(code: int, params=(), *, arch=AMD64, platform=2, pc=GAME_BASE + 0x100, sp=STACK_BASE + 0x40,
            modules=(("C:\\build\\spark_game.exe", GAME_BASE, 0x20000),), flags=0, exception=True):
    builder = MinidumpBuilder().system_info(arch, platform)
    for path, base, size in modules:
        builder.module(path, base, size)
    pointer = 4 if arch == X86 else 8
    builder.thread(7, arch=arch, pc=pc, sp=sp,
                   stack=stack_with_returns(sp, [modules[0][1] + 0x300], pointer_size=pointer))
    if exception:
        builder.exception(7, code, address=pc, params=params, flags=flags, arch=arch, pc=pc, sp=sp)
    return builder.build()


def test_fast_fail_gs_cookie():
    report = _report(_single(0xC0000409, params=(2,)))
    assert report.exception.name == "STATUS_STACK_BUFFER_OVERRUN"
    assert report.exception.detail == "FAST_FAIL_STACK_COOKIE_CHECK_FAILURE"
    assert "gs_cookie_overrun" in _hints(report)


def test_cpp_exception():
    report = _report(_single(0xE06D7363, params=(0x19930520, 0, 0)))
    assert report.exception.name == "CPP_EH_EXCEPTION"
    assert "unhandled_cpp_exception" in _hints(report)


@pytest.mark.parametrize("arch,cpu", [(ARM64, "arm64"), (X86, "x86")])
def test_arm64_and_x86_context_offsets(arch, cpu):
    pc = (0x40_1000 if arch == X86 else GAME_BASE + 0x80)
    base = 0x40_0000 if arch == X86 else GAME_BASE
    sp = 0x0019_F000 if arch == X86 else STACK_BASE
    report = _report(_single(0xC0000005, params=(0, 0), arch=arch, pc=pc, sp=sp,
                             modules=(("C:\\build\\spark_game.exe", base, 0x20000),)))
    assert report.cpu == cpu
    top = report.crashing_thread().frames[0]
    assert top.module == "spark_game.exe" and top.module_offset == pc - base and top.trust == "context"


def test_breakpad_bpel_module_and_linux_signal():
    builder = MinidumpBuilder().system_info(AMD64, LINUX, 5, 15, 0, csd="Linux 5.15.0 x86_64")
    builder.module("/opt/game/spark_game", GAME_BASE, 0x20000, cv=bpel_record(bytes(range(20))))
    builder.thread(99, pc=GAME_BASE + 0x42, sp=STACK_BASE, stack=b"\0" * 64)
    builder.exception(99, 11, address=0, flags=1, pc=GAME_BASE + 0x42, sp=STACK_BASE)
    builder.breakpad_info(98, 99)
    report = _report(builder.build())
    assert report.source_kind == "breakpad_minidump"
    assert report.exception.name == "SIGSEGV" and report.exception.detail == "SEGV_MAPERR"
    assert report.exception.access_address == 0
    assert report.modules[0].debug_id == bytes(range(20)).hex()
    assert "null_deref" in _hints(report)


def test_crashpad_annotations_are_read_and_capped():
    builder = MinidumpBuilder().system_info()
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000)
    builder.thread(1, pc=GAME_BASE, sp=STACK_BASE, stack=b"\0" * 32)
    builder.exception(1, 0xC0000005, params=(0, 0), pc=GAME_BASE, sp=STACK_BASE)
    pairs = [("build", "1234\x1b[31m\nred")] + [("k%d" % i, "v" * 400) for i in range(40)]
    builder.crashpad_annotations(pairs)
    report = _report(builder.build())
    assert report.source_kind == "crashpad_minidump"
    assert len(report.annotations) == 32
    assert report.annotations[0].key == "build" and report.annotations[0].value == "1234 red"
    assert all(len(item.value) <= 240 for item in report.annotations)
    assert any("crashpad annotations" in note for note in report.notes)


def test_no_exception_stream_gives_hang_hint():
    builder = MinidumpBuilder().system_info()
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000)
    builder.module("C:\\Windows\\System32\\ntdll.dll", NTDLL_BASE, 0x1F0000)
    builder.thread(1, pc=NTDLL_BASE + 0x100, sp=STACK_BASE, stack=b"\0" * 64)
    report = _report(builder.build())
    assert report.exception is None and report.crashing_thread_id is None
    assert "hang_or_deadlock" in _hints(report)
    assert report.signature_basis == "exception_only"


def test_memory64_list_stack_is_scanned():
    builder = MinidumpBuilder().system_info()
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000)
    sp = STACK_BASE + 0x200
    builder.thread(5, pc=GAME_BASE + 0x10, sp=sp, in_memory64=True,
                   stack=stack_with_returns(sp, [GAME_BASE + 0x777, GAME_BASE + 0x888]))
    builder.memory64(STACK_BASE + 0x10000, b"\x11" * 64)
    builder.exception(5, 0xC0000005, params=(1, 0x10), pc=GAME_BASE + 0x10, sp=sp)
    frames = _report(builder.build()).crashing_thread().frames
    assert [f.module_offset for f in frames] == [0x10, 0x777, 0x888]


def test_managed_runtime_module_is_flagged():
    data = amd64_access_violation(extra_modules=(("C:\\Program Files\\dotnet\\coreclr.dll", 0x7FFC_0000_0000, 0x100000),))
    triage = read_minidump(BytesReader(data))
    assert triage.managed_runtime
    report = triage_to_report(triage)
    module = report.module_named("coreclr.dll")
    assert module.managed_runtime and not module.in_project
    assert any("managed runtime" in note for note in report.notes)


def test_unloaded_module_frame():
    builder = MinidumpBuilder().system_info()
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000)
    builder.unloaded("C:\\build\\plugin.dll", 0x7FF7_0000_0000, 0x10000)
    builder.thread(1, pc=0x7FF7_0000_0040, sp=STACK_BASE, stack=b"\0" * 32)
    builder.exception(1, 0xC0000005, params=(8, 0x7FF7_0000_0040), pc=0x7FF7_0000_0040, sp=STACK_BASE)
    report = _report(builder.build())
    top = report.crashing_thread().frames[0]
    assert top.module == "<unloaded>plugin.dll" and top.module_offset == 0x40
    assert "execute_violation" in _hints(report)


def test_scan_stack_is_bounded():
    modules = [ModuleInfo(name="m.exe", base=0x1000, size=0x1000)]
    stack = struct.pack("<8Q", 0, 0x1100, 5, 0x1200, 0x9999, 0x1300, 0x1400, 0x1500)
    frames = scan_stack(stack, 0x100, 0x100, modules, pointer_size=8, max_frames=3)
    assert [f.module_offset for f in frames] == [0x100, 0x200, 0x300]


# ------------------------------------------------------------------ malformed input

def test_not_a_minidump():
    assert _code(b"PK\x03\x04" + b"\0" * 64) == "NOT_MINIDUMP"
    assert _code(b"MDMP") == "NOT_MINIDUMP"


def test_truncated_file():
    data = amd64_access_violation()
    assert _code(data[: len(data) // 2]) == "TRUNCATED"


def test_zero_size_directory():
    data = MinidumpBuilder().system_info().build(n_streams_override=0)
    assert _code(data) == "TRUNCATED"


def test_stream_count_all_ones():
    data = MinidumpBuilder().system_info().build(n_streams_override=0xFFFFFFFF)
    assert _code(data) == "LIMIT_EXCEEDED"


def test_thread_count_larger_than_stream():
    builder = MinidumpBuilder().system_info()
    builder.raw_stream(3, struct.pack("<I", 1000) + b"\0" * 48)
    assert _code(builder.build()) == "TRUNCATED"


def test_4097_modules_limit_exceeded():
    builder = MinidumpBuilder().system_info()
    entry = b"\0" * 108
    builder.raw_stream(4, struct.pack("<I", 4097) + entry * 4097)
    assert _code(builder.build()) == "LIMIT_EXCEEDED"


def test_module_name_rva_out_of_bounds():
    builder = MinidumpBuilder().system_info()
    entry = bytearray(108)
    struct.pack_into("<QIIII", entry, 0, GAME_BASE, 0x1000, 0, 0, 0x7FFF_FFF0)
    builder.raw_stream(4, struct.pack("<I", 1) + bytes(entry))
    assert _code(builder.build()) == "OUT_OF_BOUNDS"


def _module_with_name_blob(blob: bytes) -> bytes:
    builder = MinidumpBuilder().system_info()
    rva = builder.put(blob)
    entry = bytearray(108)
    struct.pack_into("<QIIII", entry, 0, GAME_BASE, 0x1000, 0, 0, rva)
    builder.raw_stream(4, struct.pack("<I", 1) + bytes(entry))
    return builder.build()


def test_odd_length_minidump_string():
    assert _code(_module_with_name_blob(struct.pack("<I", 7) + b"a\0b\0c\0d")) == "OUT_OF_BOUNDS"


def test_overlong_minidump_string():
    assert _code(_module_with_name_blob(struct.pack("<I", 4000) + b"a\0" * 2000)) == "LIMIT_EXCEEDED"


def test_overlapping_memory_ranges():
    builder = MinidumpBuilder().system_info()
    builder.memory(0x1000, b"\1" * 0x100).memory(0x1080, b"\2" * 0x100)
    assert _code(builder.build()) == "OUT_OF_BOUNDS"


def test_memory64_base_rva_overflow():
    builder = MinidumpBuilder().system_info()
    builder.raw_stream(9, struct.pack("<QQ", 1, 0xFFFF_FFFF_FFFF_0000) + struct.pack("<QQ", 0x1000, 0x100))
    assert _code(builder.build()) == "OUT_OF_BOUNDS"


def test_unsupported_architecture():
    assert _code(MinidumpBuilder().system_info(arch=6).build()) == "UNSUPPORTED_ARCH"


def test_read_budget_enforced():
    limits = MinidumpLimits(max_bytes_read=600)
    assert _code(amd64_access_violation(), limits) == "LIMIT_EXCEEDED"


def test_time_budget_enforced_with_injected_clock():
    ticks = iter(range(0, 10_000_000, 5))
    assert _code(amd64_access_violation(), MinidumpLimits(max_seconds=2.0), clock=lambda: next(ticks)) == \
        "TIME_EXCEEDED"


def test_many_threads_scan_only_the_threads_that_are_kept():
    # 1500 threads with 4 KiB stacks each: only the crashing thread and the
    # first MAX_OTHER_THREADS others are kept, so only their stacks are read.
    builder = MinidumpBuilder().system_info()
    builder.module("C:\\b\\spark_game.exe", GAME_BASE, 0x20000)
    for tid in range(1, 1501):
        builder.thread(tid, pc=GAME_BASE + 5, sp=STACK_BASE, stack=b"\0" * 4096)
    builder.exception(1000, 0xC0000005, params=(0, 0), pc=GAME_BASE + 5, sp=STACK_BASE)
    triage = read_minidump(BytesReader(builder.build()))
    assert triage.threads_total == 1500 and len(triage.threads) == 16
    assert triage.threads[0].thread_id == 1000 and triage.threads[0].crashed
    assert triage.truncated
    assert triage.bytes_read < 256 << 10
