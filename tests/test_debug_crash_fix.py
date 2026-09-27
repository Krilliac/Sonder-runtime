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


FUZZ_CASES = 2000

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


def test_lines_are_read_by_the_build_tools_output_parser_as_fatal_at_the_same_place():
    """The hand-off into the C++ build feature: its build-output parser (which
    attributes a build_job/build_fix report) reads the crash lines as fatal
    diagnostics at the same file and line, so a fix loop sees them as it
    sees a compiler's."""
    from sonder_runtime.domain.build import output as build_output

    report = _report(MAPPED, hints=(CauseHint("null_deref", "high", "0x0"),))
    dset = build_output.parse_build_diagnostics("\n".join(cf.crash_diagnostic_lines(report)))
    assert [(d.severity, d.file, d.line) for d in dset.diagnostics] == [
        ("fatal", "src/game/player.cpp", 42), ("fatal", "src/game/world.cpp", 118)]
    assert not dset.truncated


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
    # the next step names the console's build-fix command, which the REPL
    # routes to the build feature (a real command, not a raw tool name)
    from sonder_runtime.interfaces.repl.facades.build_tools import BUILD_COMMAND_SPECS

    commands = {spec.name for spec in BUILD_COMMAND_SPECS}
    assert "/fix-build <target>" in brief and "/fix-build" in commands
    assert "/build run compile <file>" in brief and "/build" in commands
    assert "build_job" not in brief


# --- hostile input: containment, redaction, escapes, fuzz ----------------------------


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "/etc/passwd", "C:/Windows/win.ini", "c:\\x\\y.cpp",
    "\\\\host\\share\\a.cpp", "~/src/a.cpp", "src/../../a.cpp", "", "..",
])
def test_a_lookup_or_report_cannot_name_a_file_outside_the_checkout(bad):
    lookup = cf.project_source_lookup(lambda path: bad, lambda local: ["secret"])
    frames = (_frame(0, "f", "/ci/a.cpp", 3),)
    handoff = cf.build_crash_fix_handoff(_report(frames), source_lookup=lookup)
    assert handoff.diagnostics == () and handoff.source is None
    assert "secret" not in cf.render_crash_fix_brief(handoff)
    # A report that already carries a non-relative local_file gives nothing either.
    premapped = (_frame(0, "f", "/ci/a.cpp", 3, in_project=True, local_file=bad),)
    assert cf.crash_diagnostics(_report(premapped)) == ()


def test_excerpt_lines_lose_terminal_escapes_and_bidi_overrides():
    source = ["int a;\x1b[2J\x1b]0;pwned\x07", "\tp->x = 1; \u202e// }", "\x9b31mz\x00"]
    lookup = cf.project_source_lookup(lambda p: "src/a.cpp", lambda local: source)
    handoff = cf.build_crash_fix_handoff(_report((_frame(0, "f", "/ci/a.cpp", 2),)),
                                         source_lookup=lookup)
    assert handoff.source.excerpt == ("int a;", "    p->x = 1; // }", "31mz")
    brief = cf.render_crash_fix_brief(handoff)
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u202a-\u202e]", brief)


def test_user_paths_and_names_never_reach_the_brief():
    frames = (
        _frame(0, "main", "/home/natew/proj/src/m.cpp", 3,
               module="/home/natew/proj/build/app"),
        _frame(1, "run", "C:\\Users\\Nate\\src\\game\\w.cpp", 9,
               module="C:\\Users\\Nate\\build\\game.exe"),
    )
    report = _report(frames, process="/home/natew/proj/build/app",
                     hints=(CauseHint("null_deref", "high", "read of /root/x and "
                                      "\\\\buildhost\\share\\a.pdb"),))
    brief = cf.render_crash_fix_brief(cf.build_crash_fix_handoff(report), run_id="r1")
    for leaked in ("natew", "Nate", "/root/", "buildhost"):
        assert leaked not in brief
    assert "app!main (~/proj/src/m.cpp:3)" in brief and "in app thread 7" in brief


def test_fuzzed_reports_are_bounded_and_never_escape_the_checkout():
    import random
    import time

    rng = random.Random(0xC0DE)
    alphabet = ("a", "Z", "/", "\\", "..", ":", "\n", "\r", "\x1b[31m", "\x9b", "\x00",
                "\u202e", " /home/eve/", " C:\\Users\\eve\\", " ", ";", "$(id)", "%s", "{}",
                "\ud800", "\U0001f4a5")

    pool = ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 700)))
            for _ in range(400)]

    def junk(limit):
        return rng.choice(pool)[:rng.randrange(0, limit)]

    def value():
        return rng.choice([None, True, False, -1, 0, 10 ** 30, 3, 1.5, junk(40), b"\xff"])

    # Where a frame's local path comes from: the service's source map (fine),
    # or anything a hostile report/lookup could claim (refused).
    LOCALS = (None, "src/a.cpp", "src\\b.cpp", "../x", "/home/eve/a.cpp", "C:\\Users\\eve\\a.cpp",
              "a.cpp:stream", "\\\\eve\\share\\a.cpp", "~eve/a.cpp", "src/.../a.cpp", " ")
    started = time.monotonic()
    for _ in range(FUZZ_CASES):
        frames = tuple(
            _frame(i, junk(60) if rng.random() < .8 else "x" * 200_000,
                   junk(80), value(), in_project=rng.random() < .5,
                   local_file=rng.choice(LOCALS),
                   column=value(), module=junk(50))
            for i in range(rng.randrange(0, 40)))
        exception = CrashException(junk(10), junk(90), junk(10), value(), junk(10),
                                   value(), value())
        report = _report(frames, process=junk(90), exception=exception,
                         hints=tuple(CauseHint(junk(20), junk(8), junk(300))
                                     for _ in range(rng.randrange(0, 12))))
        lines = rng.sample(pool, rng.randrange(0, 80))
        lookup = cf.project_source_lookup(
            lambda p: rng.choice(LOCALS), lambda local: lines)
        handoff = cf.build_crash_fix_handoff(report, source_lookup=lookup)
        brief = cf.render_crash_fix_brief(handoff, run_id=junk(100))
        assert len(brief) <= cf.MAX_BRIEF_CHARS + 20
        for bad in ("\x1b", "\x00", "\x9b", "\u202e", "\ud800", "eve"):
            assert bad not in brief, (bad, brief[max(0, brief.find(bad) - 80):brief.find(bad) + 40])
        brief.encode("utf-8")
        cf.crash_failure_observation(handoff)
        assert len(handoff.diagnostics) <= cf.MAX_DIAGNOSTICS
        for diagnostic in handoff.diagnostics:
            assert cf.project_relative(diagnostic.file) == diagnostic.file
        if handoff.source is not None:
            assert len(handoff.source.excerpt) <= cf.MAX_EXCERPT_LINES
    assert time.monotonic() - started < 120
