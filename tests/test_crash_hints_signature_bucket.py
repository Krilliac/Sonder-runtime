"""Cause hints, signature bases, bucketing and Tier-1 merge."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sonder_runtime.domain.binaries.reader import BytesReader
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.crash.bucket import bucket_reports
from sonder_runtime.domain.crash.debugger_text import (
    DebuggerFindings, SymbolizedAddress, parse_cdb, parse_gdb,
)
from sonder_runtime.domain.crash.hints import crash_signature, derive_hints, finalize_report, strip_function_name
from sonder_runtime.domain.crash.minidump import read_minidump, triage_to_report
from sonder_runtime.domain.crash.model import (
    CrashException, CrashReport, ModuleInfo, StackFrame, ThreadSummary,
)
from sonder_runtime.domain.crash.render import merge_findings
from tests.support.minidump_builder import (
    AMD64, GAME_BASE, RENDER_BASE, STACK_BASE, MinidumpBuilder, amd64_access_violation,
)
from tests.test_crash_elf_core import build_core


FIXTURES = Path(__file__).parent / "fixtures" / "crash"
NONCE = "0123456789abcdef"


def _dump_report(data: bytes, label: str = "x.dmp") -> CrashReport:
    return triage_to_report(read_minidump(BytesReader(data)), source_label=label)


def _kinds(report) -> set[str]:
    return {hint.kind for hint in report.hints}


def test_strip_function_name():
    assert strip_function_name("ns::Foo<std::vector<int>>::bar(int, char) const+0x12") == "ns::Foo::bar"
    assert strip_function_name("(anonymous namespace)::run(void)") == "(anonymous namespace)::run"
    assert strip_function_name("Vec::operator<(Vec const&)") == "Vec::operator<"
    assert strip_function_name("Functor::operator()(int)") == "Functor::operator()"


def test_same_crash_symbolicated_and_unsymbolicated_bases():
    base = _dump_report(amd64_access_violation())
    assert base.signature_basis == "module_offsets"
    merged = merge_findings(base, parse_cdb((FIXTURES / "cdb_av.txt").read_text(), NONCE), "cdb")
    assert merged.signature_basis == "functions"
    assert merged.engines == ("pure", "cdb")
    top = merged.crashing_thread().frames[0]
    assert top.function == "Renderer::Submit" and top.module == "spark_game.exe" and top.in_project
    # Module identity and versions stay from the pure reader.
    assert merged.modules[0].debug_id == base.modules[0].debug_id and merged.modules[0].version == "1.2.3.4"
    assert merged.symbolication == "full"
    assert merged.exception.access_address == 0  # pure value kept


def test_different_modules_unsymbolicated_give_different_signatures():
    one = _dump_report(amd64_access_violation(crash_module="spark_game.exe"))
    two = _dump_report(amd64_access_violation(crash_module="render.dll"))
    assert one.signature_basis == two.signature_basis == "module_offsets"
    assert one.signature != two.signature


def test_exception_only_basis_when_no_project_frames():
    report = CrashReport(source_kind="elf_core", exception=CrashException(name="SIGABRT", signal="SIGABRT"),
                         threads=(ThreadSummary(1, crashed=True, frames=(StackFrame(0, module="libc.so.6"),)),))
    signature, basis = crash_signature(report)
    assert basis == "exception_only" and len(signature) == 16


def test_signature_ignores_crt_startup_frames():
    def report(frames):
        return finalize_report(CrashReport(
            source_kind="elf_core", exception=CrashException(name="SIGSEGV", signal="SIGSEGV"),
            threads=(ThreadSummary(1, crashed=True, frames=tuple(frames)),)))
    common = [StackFrame(0, function="run_frame(Renderer*, int)", in_project=True),
              StackFrame(1, function="main", in_project=True)]
    with_start = common + [StackFrame(2, function="_start", in_project=True)]
    assert report(common).signature == report(with_start).signature


def test_bucket_counts_and_labels():
    null_a = _dump_report(amd64_access_violation(), "a.dmp")
    null_b = _dump_report(amd64_access_violation(), "b.dmp")
    other = _dump_report(amd64_access_violation(crash_module="render.dll"), "c.dmp")
    buckets = bucket_reports([null_a, other, null_b])
    assert [(b.count, b.sample_labels) for b in buckets] == [(2, ("a.dmp", "b.dmp")), (1, ("c.dmp",))]
    assert buckets[0].top_frame == "spark_game.exe+0x1234"
    with pytest.raises(InvalidInput):
        bucket_reports([null_a] * 65)


def test_hang_and_gpu_hints():
    builder = MinidumpBuilder().system_info(AMD64)
    builder.module("C:\\build\\spark_game.exe", GAME_BASE, 0x20000)
    builder.module("C:\\Windows\\System32\\DriverStore\\nvwgf2umx.dll", RENDER_BASE, 0x100000)
    builder.thread(1, pc=RENDER_BASE + 0x40, sp=STACK_BASE, stack=b"\0" * 64)
    builder.exception(1, 0x887A0005, pc=RENDER_BASE + 0x40, sp=STACK_BASE)
    gpu = _dump_report(builder.build())
    assert ("gpu_driver", "medium") in {(h.kind, h.confidence) for h in gpu.hints}
    waits = replace(gpu, exception=None, threads=(ThreadSummary(1, frames=(
        StackFrame(0, module="ntdll.dll", function="NtWaitForSingleObject"),)),))
    hints = {(h.kind, h.confidence) for h in derive_hints(waits)}
    assert ("hang_or_deadlock", "medium") in hints


def test_pure_virtual_and_divide_by_zero_hints():
    report = CrashReport(source_kind="windows_minidump",
                         exception=CrashException(code="0xC0000094", name="EXCEPTION_INT_DIVIDE_BY_ZERO"),
                         threads=(ThreadSummary(1, crashed=True, frames=(
                             StackFrame(0, function="_purecall"),)),))
    assert {"divide_by_zero", "pure_virtual_call"} <= {h.kind for h in derive_hints(report)}


def test_merge_gdb_over_core_keeps_pure_identity():
    from sonder_runtime.domain.crash.elf_core import core_to_report, read_elf_core

    base = core_to_report(read_elf_core(BytesReader(build_core())))
    text = "\n".join([
        "SONDER_%s_BT" % NONCE,
        "#0  0x0000555555555266 in run_frame (renderer=0x0) at /src/sparkmini/crasher.cpp:20",
        "#1  0x00005555555553a9 in main (argc=2) at /src/sparkmini/crasher.cpp:47",
        "SONDER_%s_THREADS" % NONCE,
        "Thread 2 (Thread 0x1 (LWP 4243)):",
        "#0  0x00007ffff7809000 in futex_wait () from /lib/libc.so.6",
        "Thread 1 (Thread 0x2 (LWP 4242)):",
        "#0  0x0000555555555266 in run_frame (renderer=0x0) at /src/sparkmini/crasher.cpp:20",
        "SONDER_%s_END" % NONCE,
    ])
    merged = merge_findings(base, parse_gdb(text, NONCE), "gdb")
    top = merged.crashing_thread().frames[0]
    assert (top.function, top.module, top.module_offset, top.line) == ("run_frame", "crasher", 0x1266, 20)
    assert merged.crashing_thread().thread_id == 4242
    assert merged.threads[1].frames[0].function == "futex_wait"
    assert merged.modules[0].debug_id == base.modules[0].debug_id
    assert merged.signature_basis == "functions" and merged.symbolication == "full"


def test_merge_symbolizer_expands_inline_frames():
    base = _dump_report(amd64_access_violation())
    findings = DebuggerFindings(symbolized=(SymbolizedAddress("spark_game.exe", 0x1234, (
        StackFrame(0, function="Renderer::Validate", file="render.h", line=17, inline=True, trust="symbolizer"),
        StackFrame(1, function="Renderer::Submit", file="render.cpp", line=42, trust="symbolizer"))),))
    merged = merge_findings(base, findings, "llvm_symbolizer")
    frames = merged.crashing_thread().frames
    assert [f.function for f in frames[:2]] == ["Renderer::Validate", "Renderer::Submit"]
    assert frames[0].inline and frames[0].module_offset == 0x1234 and frames[0].in_project
    assert merged.symbolication == "partial"


def test_merge_with_empty_findings_notes_it():
    base = _dump_report(amd64_access_violation())
    merged = merge_findings(base, DebuggerFindings(sections_seen=("BEGIN",)), "cdb")
    assert merged.crashing_thread().frames == base.crashing_thread().frames
    assert any("cdb produced no frames" in note for note in merged.notes)


def test_merge_updates_symbol_status_only():
    base = _dump_report(amd64_access_violation())
    findings = DebuggerFindings(modules=(ModuleInfo(name="spark_game", symbols="mismatch", version="9.9"),))
    merged = merge_findings(base, findings, "cdb")
    module = merged.module_named("spark_game.exe")
    assert module.symbols == "mismatch" and module.version == "1.2.3.4"
