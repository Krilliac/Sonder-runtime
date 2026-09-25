"""Seeded mutation fuzzing of every pure crash parser (SEC-008).

Contract: a hostile input yields either the parser's typed format error or a
valid report, within the time and read budgets. Nothing else may escape.
Each input is timed; the p99 must stay under 50 ms and no single input may
take longer than 0.5 s (headroom for scheduler noise on shared CI CPUs).
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from sonder_runtime.domain.binaries.pdb_info import read_pdb_identity
from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
from sonder_runtime.domain.binaries.reader import BinaryFormatError, BytesReader
from sonder_runtime.domain.crash.apple_ips import parse_apple_ips
from sonder_runtime.domain.crash.debugger_text import (
    parse_cdb, parse_eu_stack, parse_gdb, parse_lldb, parse_stackwalk_json, parse_stackwalk_machine,
    parse_symbolizer_json,
)
from sonder_runtime.domain.crash.elf_core import CoreLimits, core_to_report, read_elf_core
from sonder_runtime.domain.crash.minidump import MinidumpLimits, read_minidump, triage_to_report
from sonder_runtime.domain.crash.model import CaptureFormatError, CrashReport
from sonder_runtime.domain.crash.render import render_report, report_to_wire
from sonder_runtime.domain.crash.sanitizer import parse_sanitizer_report
from sonder_runtime.domain.crash.valgrind_xml import parse_valgrind_xml
from tests.support.minidump_builder import (
    ARM64, GAME_BASE, LINUX, STACK_BASE, MinidumpBuilder, amd64_access_violation, bpel_record, stack_with_returns,
)
from tests.test_crash_elf_core import build_core


FIXTURES = Path(__file__).parent / "fixtures" / "crash"
BINARY_ITERATIONS = 3000
TEXT_ITERATIONS = 2000
NONCE = "0123456789abcdef"
_BOUNDARY = (0, 1, 0x7F, 0x80, 0xFF, 0x7FFF_FFFF, 0x8000_0000, 0xFFFF_FFFF)


def _minidump_seeds() -> list[bytes]:
    fast_fail = MinidumpBuilder().system_info()
    fast_fail.module("C:\\b\\spark_game.exe", GAME_BASE, 0x20000)
    fast_fail.thread(1, pc=GAME_BASE + 5, sp=STACK_BASE, stack=stack_with_returns(STACK_BASE, [GAME_BASE + 9]))
    fast_fail.exception(1, 0xC0000409, params=(2,), pc=GAME_BASE + 5, sp=STACK_BASE)
    arm = MinidumpBuilder().system_info(ARM64)
    arm.module("C:\\b\\spark_game.exe", GAME_BASE, 0x20000)
    arm.thread(2, arch=ARM64, pc=GAME_BASE + 0x40, sp=STACK_BASE, stack=b"\0" * 64)
    arm.exception(2, 0xC0000005, params=(0, 0), arch=ARM64, pc=GAME_BASE + 0x40, sp=STACK_BASE)
    breakpad = MinidumpBuilder().system_info(platform=LINUX)
    breakpad.module("/opt/g/spark_game", GAME_BASE, 0x1000, cv=bpel_record(b"\x01" * 20))
    breakpad.thread(3, pc=GAME_BASE, sp=STACK_BASE, stack=b"\0" * 32)
    breakpad.exception(3, 11, flags=1, pc=GAME_BASE, sp=STACK_BASE).breakpad_info(3, 3)
    crashpad = MinidumpBuilder().system_info()
    crashpad.module("C:\\b\\spark_game.exe", GAME_BASE, 0x20000)
    crashpad.thread(4, pc=GAME_BASE, sp=STACK_BASE, stack=b"\0" * 32).thread_name(4, "Main")
    crashpad.crashpad_annotations([("ver", "1.0"), ("gpu", "none")]).unloaded("C:\\p.dll", 0x10000, 0x1000)
    mem64 = MinidumpBuilder().system_info()
    mem64.module("C:\\b\\spark_game.exe", GAME_BASE, 0x20000)
    mem64.thread(5, pc=GAME_BASE, sp=STACK_BASE, in_memory64=True,
                 stack=stack_with_returns(STACK_BASE, [GAME_BASE + 0x10]))
    mem64.memory(0x9000, b"\1" * 32).misc(77)
    return [amd64_access_violation(), fast_fail.build(), arm.build(), breakpad.build(), crashpad.build(),
            mem64.build()]


def _mutate(rng: random.Random, seed: bytes, seeds: list[bytes]) -> bytes:
    data = bytearray(seed)
    op = rng.randrange(5)
    if op == 0 or not data:
        for _ in range(rng.randint(1, 8)):
            if data:
                data[rng.randrange(len(data))] = rng.randrange(256)
    elif op == 1:
        del data[rng.randrange(len(data) + 1):]
    elif op == 2:
        donor = rng.choice(seeds)
        start = rng.randrange(len(donor) + 1)
        chunk = donor[start:start + rng.randint(1, 256)]
        at = rng.randrange(len(data) + 1)
        data[at:at + len(chunk)] = chunk
    elif op == 3 and len(data) >= 4:
        at = rng.randrange(0, len(data) - 3) & ~3
        data[at:at + 4] = rng.choice(_BOUNDARY).to_bytes(4, "little")
    else:
        at = rng.randrange(len(data) + 1)
        data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 32)))
    return bytes(data)


def _check_timings(timings: list[float]) -> None:
    timings.sort()
    p99 = timings[int(len(timings) * 0.99) - 1]
    assert p99 < 0.05, "p99 %.1f ms" % (p99 * 1000)
    assert timings[-1] < 0.5, "max %.1f ms" % (timings[-1] * 1000)


def _valid_report(report: CrashReport) -> None:
    assert isinstance(report, CrashReport)
    json.dumps(report_to_wire(report))
    render_report(report)


def _run_binary(seeds: list[bytes], parse, allowed, *, iterations: int, salt: int) -> None:
    timings = []
    outcomes = {"ok": 0, "error": 0}
    for index, seed in enumerate(seeds):
        rng = random.Random(salt * 1000 + index)
        for _ in range(iterations):
            data = _mutate(rng, seed, seeds)
            started = time.perf_counter()
            try:
                parse(data)
                outcomes["ok"] += 1
            except allowed:
                outcomes["error"] += 1
            timings.append(time.perf_counter() - started)
    _check_timings(timings)
    assert outcomes["ok"] and outcomes["error"]


def test_fuzz_minidump():
    limits = MinidumpLimits()

    def parse(data: bytes) -> None:
        triage = read_minidump(BytesReader(data), limits)
        assert triage.bytes_read <= limits.max_bytes_read
        _valid_report(triage_to_report(triage))

    _run_binary(_minidump_seeds(), parse, CaptureFormatError, iterations=BINARY_ITERATIONS, salt=1)


def test_fuzz_elf_core():
    limits = CoreLimits()

    def parse(data: bytes) -> None:
        triage = read_elf_core(BytesReader(data), limits)
        assert triage.bytes_read <= limits.max_bytes_read
        _valid_report(core_to_report(triage))

    _run_binary([build_core(), build_core(machine=183, pn_xnum=True)], parse, CaptureFormatError,
                iterations=BINARY_ITERATIONS, salt=2)


def test_fuzz_pe_identity():
    seeds = [(FIXTURES / "pe" / v / "spark_tiny.exe").read_bytes() for v in ("a", "b")]
    _run_binary(seeds, lambda data: read_pe_identity(BytesReader(data)), BinaryFormatError,
                iterations=BINARY_ITERATIONS, salt=3)


def test_fuzz_pdb_identity():
    seeds = [(FIXTURES / "pe" / v / "spark_tiny.pdb").read_bytes() for v in ("a", "b")]

    def mutate_header_heavy(data: bytes) -> None:
        read_pdb_identity(BytesReader(data))

    _run_binary(seeds, mutate_header_heavy, BinaryFormatError, iterations=BINARY_ITERATIONS, salt=4)


def _mutate_text(rng: random.Random, text: str) -> str:
    lines = text.splitlines(keepends=True) or [""]
    op = rng.randrange(6)
    if op == 0:
        chars = list(text)
        for _ in range(rng.randint(1, 10)):
            if chars:
                chars[rng.randrange(len(chars))] = rng.choice("0x#:()[]{}\"'\\\n\r\x1b\x00 SONDER_")
        return "".join(chars)
    if op == 1:
        return text[: rng.randrange(len(text) + 1)]
    if op == 2:
        index = rng.randrange(len(lines))
        return "".join(lines[:index] + [lines[index]] * rng.randint(2, 50) + lines[index:])
    if op == 3:
        rng.shuffle(lines)
        return "".join(lines)
    if op == 4:
        index = rng.randrange(len(lines))
        return "".join(lines[:index] + [lines[index][:1] + "9" * rng.randint(10, 5000) + "\n"] + lines[index + 1:])
    return "".join(line for line in lines if rng.random() > 0.2)


TEXT_CASES = [
    ("sanitizer", ["asan_uaf.log", "tsan.log", "ubsan.log"], lambda t: parse_sanitizer_report(t), CaptureFormatError),
    ("valgrind", ["memcheck.xml", "memcheck_uaf.xml"], lambda t: parse_valgrind_xml(t.encode("utf-8", "replace")),
     CaptureFormatError),
    ("ips", ["sample.ips"], lambda t: parse_apple_ips(t), CaptureFormatError),
    ("gdb", ["gdb_segv.txt"], lambda t: parse_gdb(t, NONCE), ()),
    ("lldb", ["lldb_bt.txt"], lambda t: parse_lldb(t, NONCE), ()),
    ("cdb", ["cdb_av.txt", "cdb_gs.txt"], lambda t: parse_cdb(t, NONCE), ()),
    ("eu_stack", ["eu_stack.txt"], lambda t: parse_eu_stack(t), ()),
    ("stackwalk_json", ["stackwalk_json.txt"], lambda t: parse_stackwalk_json(t), CaptureFormatError),
    ("stackwalk_m", ["stackwalk_m.txt"], lambda t: parse_stackwalk_machine(t), ()),
    ("symbolizer", ["symbolizer_json.txt"], lambda t: parse_symbolizer_json(t), CaptureFormatError),
]


@pytest.mark.parametrize("name,files,parse,allowed", TEXT_CASES, ids=[case[0] for case in TEXT_CASES])
def test_fuzz_text_parsers(name, files, parse, allowed):
    seeds = [(FIXTURES / item).read_text() for item in files]
    rng = random.Random(name)
    timings = []
    per_seed = max(1, TEXT_ITERATIONS // len(seeds))
    for seed in seeds:
        for _ in range(per_seed):
            text = _mutate_text(rng, seed)
            started = time.perf_counter()
            try:
                result = parse(text)
                if isinstance(result, CrashReport):
                    _valid_report(result)
            except allowed or ():
                pass
            timings.append(time.perf_counter() - started)
    _check_timings(timings)
