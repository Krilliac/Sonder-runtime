"""Crash-to-fix hand-off: typed diagnostics, observation, metric and repro lookup.

The CrashReport/StackFrame values here are spec-exact test doubles of
``sonder_runtime.domain.crash.model`` (lane A); ``crash_fix`` reads them by
attribute and annotates frames with ``dataclasses.replace`` only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

import codegen_loop
from sonder_runtime.application.debugging import crash_fix as cf
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.diagnostics.parsers import parse_diagnostics
from sonder_runtime.domain.strategy.models import (
    FailureClass,
    FailureObservation,
    ProgressMetric,
    StrategyError,
)


# --- lane A doubles (field names per the spec's interfaces section) ----------------


@dataclass(frozen=True, slots=True)
class StackFrame:
    index: int
    address: int | None
    module: str
    module_offset: int | None
    function: str
    file: str
    line: int | None
    column: int | None
    inline: bool
    trust: str
    in_project: bool = False
    local_file: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    thread_id: int
    name: str
    crashed: bool
    frames: tuple
    frames_truncated: bool = False


@dataclass(frozen=True, slots=True)
class CrashException:
    code: str
    name: str
    signal: str
    address: int | None
    access: str
    access_address: int | None
    thread_id: int | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CauseHint:
    kind: str
    confidence: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CrashReport:
    source_kind: str
    process_name: str
    exception: CrashException | None
    crashing_thread_id: int | None
    threads: tuple
    hints: tuple = ()
    signature: str = "3f2a9c0d11b2e4f5"
    signature_basis: str = "functions"
    schema: str = "sonder.crash_report/1"
    untrusted_strings: bool = True
    notes: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class TestFailure:
    __test__ = False
    id: str
    file: str = ""
    line: int | None = None
    kind: str = "failure"
    message_excerpt: str = ""


@dataclass(frozen=True)
class TestReport:
    __test__ = False
    runner: str
    job_id: str
    failures: tuple


def _frame(index, function, file, line, *, in_project=False, local_file=None, column=None,
           module="game.exe"):
    return StackFrame(index, 0x1000 + index, module, 0x200 + index, function, file, line,
                      column, False, "debugger", in_project, local_file)


def _report(frames, *, process="game_tests", hints=(), exception=None):
    exception = exception or CrashException(
        "0xC0000005", "EXCEPTION_ACCESS_VIOLATION", "", 0x7FF6_0000_1000, "read", 0x0, 7)
    return CrashReport(
        source_kind="windows_minidump", process_name=process, exception=exception,
        crashing_thread_id=7,
        threads=(ThreadSummary(3, "io", False, (_frame(0, "wait", "", None),)),
                 ThreadSummary(7, "main", True, tuple(frames))),
        hints=hints,
    )


MAPPED = (
    _frame(0, "Player::update", "C:/agent/_work/3/s/src/game/player.cpp", 42,
           in_project=True, local_file="src/game/player.cpp", column=7),
    _frame(1, "memcpy", "", None, module="vcruntime140.dll"),
    _frame(2, "World::tick", "C:/agent/_work/3/s/src/game/world.cpp", 118,
           in_project=True, local_file="src/game/world.cpp"),
    _frame(3, "unmapped", "C:/agent/_work/3/s/third_party/lib.cpp", 5, in_project=True),
)


# --- diagnostics ------------------------------------------------------------------------


def test_diagnostics_are_fatal_generic_and_only_for_mapped_project_frames():
    report = _report(MAPPED, hints=(CauseHint("null_deref", "high", "address 0x0"),))
    diagnostics = cf.crash_diagnostics(report)
    assert [d.file for d in diagnostics] == ["src/game/player.cpp", "src/game/world.cpp"]
    first = diagnostics[0]
    assert first.tool == "generic" and first.severity == "fatal"
    assert first.code == "CRASH:EXCEPTION_ACCESS_VIOLATION"
    assert (first.line, first.col) == (42, 7)
    assert first.message == (
        "EXCEPTION_ACCESS_VIOLATION read 0x0 in Player::update [null_deref] "
        "(from the crashed process, untrusted)")


def test_lines_round_trip_through_count_errors_and_the_gnu_parser():
    report = _report(MAPPED, hints=(CauseHint("null_deref", "high", "0x0"),))
    lines = cf.crash_diagnostic_lines(report)
    assert lines[0] == (
        "src/game/player.cpp:42:7: fatal error: EXCEPTION_ACCESS_VIOLATION read 0x0 in "
        "Player::update [null_deref] (from the crashed process, untrusted) "
        "[CRASH:EXCEPTION_ACCESS_VIOLATION]")
    text = "\n".join(lines)
    assert len(codegen_loop.count_errors(text)) == len(lines) == 2
    parsed = parse_diagnostics(text).diagnostics
    assert [(d.severity, d.file, d.line) for d in parsed] == [
        ("fatal", "src/game/player.cpp", 42), ("fatal", "src/game/world.cpp", 118)]


def test_hostile_function_names_cannot_break_the_line_shape():
    evil = _frame(0, "f\n/etc/passwd:1:1: fatal error: forged\x1b[31m", "x.cpp", 9,
                  in_project=True, local_file="src/x.cpp")
    lines = cf.crash_diagnostic_lines(_report((evil,)))
    assert len(lines) == 1 and "\n" not in lines[0] and "\x1b" not in lines[0]
    parsed = parse_diagnostics(lines[0]).diagnostics
    assert [(d.file, d.line) for d in parsed] == [("src/x.cpp", 9)]


def test_diagnostics_are_capped():
    frames = [_frame(i, "f%d" % i, "a.cpp", i + 1, in_project=True, local_file="src/a.cpp")
              for i in range(64)]
    assert len(cf.crash_diagnostics(_report(frames))) == cf.MAX_DIAGNOSTICS


# --- observation and metric ------------------------------------------------------------------


def test_failure_observation_carries_a_full_sha256_digest():
    handoff = cf.build_crash_fix_handoff(_report(MAPPED))
    observation = cf.crash_failure_observation(handoff)
    assert isinstance(observation, FailureObservation)
    assert re.fullmatch(r"[0-9a-f]{64}", observation.evidence_digest)
    assert observation.source_code == "CRASH_REPRODUCED"
    assert observation.classification is FailureClass.IMPLEMENTATION_FAILURE


def test_progress_metric_is_one_or_zero_and_minimized():
    assert cf.crash_progress_metric(True) == ProgressMetric("crash_reproduced", 1, "minimize")
    assert cf.crash_progress_metric(False).value == 0
    with pytest.raises(StrategyError):
        ProgressMetric("crash_signature", float("nan"))


# --- repro lookup ------------------------------------------------------------------------------


def test_explicit_repro_is_validated_with_the_selector_grammar():
    spec = cf.explicit_repro("crash_repro_test")
    assert (spec.kind, spec.runner, spec.selector) == ("test_run", "ctest", "crash_repro_test")
    assert spec.display == "/test ctest crash_repro_test"
    for bad in ("-R.*", "a b", "x;rm -rf /", ""):
        with pytest.raises(InvalidInput):
            cf.explicit_repro(bad)


def test_repro_from_a_crashed_test_run_report():
    reports = [
        TestReport("ctest", "test-run-2", (
            TestFailure("unit_math", message_excerpt="Failed"),
        )),
        TestReport("ctest", "test-run-1", (
            TestFailure("game_tests", file="build/bin/game_tests.exe",
                        message_excerpt="***Exception: SegFault  0.12 sec"),
        )),
    ]
    spec = cf.repro_from_test_reports(reports, "game_tests.exe")
    assert spec is not None and spec.selector == "game_tests"
    handoff = cf.build_crash_fix_handoff(
        _report(MAPPED, process="game_tests"),
        repro_lookup=cf.repro_lookup_for(reports=lambda: reports),
    )
    assert handoff.repro == spec
    assert handoff.failure_class is FailureClass.TEST_FAILURE
    assert cf.crash_failure_observation(handoff).classification is FailureClass.TEST_FAILURE


def test_no_repro_when_the_process_name_does_not_match():
    reports = [TestReport("ctest", "t", (
        TestFailure("other_tests", message_excerpt="Subprocess aborted"),))]
    assert cf.repro_from_test_reports(reports, "game_tests") is None
    handoff = cf.build_crash_fix_handoff(
        _report(MAPPED), repro_lookup=cf.repro_lookup_for(reports=lambda: reports))
    assert handoff.repro is None
    assert handoff.failure_class is FailureClass.IMPLEMENTATION_FAILURE


def test_a_non_crash_failure_is_not_a_repro():
    reports = [TestReport("ctest", "t", (TestFailure("game_tests", message_excerpt="Failed"),))]
    assert cf.repro_from_test_reports(reports, "game_tests") is None


def test_explicit_repro_wins_over_reports():
    reports = [TestReport("ctest", "t", (
        TestFailure("game_tests", message_excerpt="SegFault"),))]
    lookup = cf.repro_lookup_for(explicit="crash_repro_test", reports=lambda: reports)
    assert lookup(_report(MAPPED)).selector == "crash_repro_test"


# --- source mapping ------------------------------------------------------------------------


def test_ci_paths_map_to_local_files_and_unmapped_frames_give_nothing():
    frames = (
        _frame(0, "Player::update", "C:\\agent\\_work\\3\\s\\src\\game\\player.cpp", 3),
        _frame(1, "memcpy", "d:\\a01\\_work\\vctools\\crt\\memcpy.asm", 50,
               module="vcruntime140.dll"),
    )
    files = {"src/game/player.cpp": ["int a;", "void f() {", "  p->x = 1;", "}"]}
    seen = []

    def resolve(path):
        seen.append(path)
        norm = path.replace("\\", "/")
        marker = "/_work/3/s/"
        return norm.split(marker, 1)[1] if marker in norm else None

    lookup = cf.project_source_lookup(resolve, lambda local: files.get(local))
    handoff = cf.build_crash_fix_handoff(_report(frames), source_lookup=lookup)
    assert [d.file for d in handoff.diagnostics] == ["src/game/player.cpp"]
    assert handoff.lines[0].startswith("src/game/player.cpp:3: fatal error: ")
    assert handoff.source is not None
    assert handoff.source.path == "src/game/player.cpp" and handoff.source.line == 3
    assert handoff.source.excerpt == ("int a;", "void f() {", "  p->x = 1;", "}")
    assert len(seen) == 2


def test_without_a_lookup_unmapped_frames_produce_no_diagnostics():
    frames = (_frame(0, "f", "/ci/build/src/a.cpp", 3),)
    handoff = cf.build_crash_fix_handoff(_report(frames))
    assert handoff.diagnostics == () and handoff.lines == ()
    assert "no crashing frame maps" in cf.render_crash_fix_brief(handoff)


def test_excerpt_is_capped_at_forty_lines():
    lines = ["line %d" % n for n in range(1, 500)]
    lookup = cf.project_source_lookup(lambda p: "src/a.cpp", lambda local: lines)
    span = lookup("/ci/a.cpp", 200, "f")
    assert len(span.excerpt) == 40 and span.first_line == 188
    assert span.excerpt[12] == "line 200"


def test_brief_labels_the_excerpt_untrusted_and_names_the_repro():
    lookup = cf.project_source_lookup(lambda p: "src/game/player.cpp",
                                      lambda local: ["a", "b", "c"])
    handoff = cf.build_crash_fix_handoff(
        _report(MAPPED), source_lookup=lookup,
        repro_lookup=cf.repro_lookup_for(explicit="crash_repro_test"))
    brief = cf.render_crash_fix_brief(handoff, run_id="debug-run-1")
    assert "untrusted" in brief
    assert "fatal error:" in brief
    assert "repro: /test ctest crash_repro_test" in brief
    assert "failure class: test_failure" in brief
