"""perf report text and callgrind.out readers over committed fixtures.

perf_report_*.txt were produced on this host from a real ``perf record -e
cpu-clock -g`` of tests/fixtures/profiling/hot.cpp with the host-owned report
templates; callgrind.out.hot is a real callgrind run of the same program
(paths rewritten to /work/spark); callgrind.out.handmade exercises
subposition compression, jumps, recursion levels and an undefined id.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.domain.profiling.callgrind import CallgrindLimits, parse_callgrind
from sonder_runtime.domain.profiling.model import (
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
)
from sonder_runtime.domain.profiling.perf_text import (
    merge_perf_digests,
    parse_perf_flat,
    parse_perf_folded,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profiling"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_perf_folded_report_with_header_metadata():
    digest = parse_perf_folded(_text("perf_report_folded.txt"))
    assert digest.source_kind == "perf_text" and digest.engines == ("perf",)
    assert digest.metadata.tool == "perf" and digest.metadata.event == "cpu-clock"
    assert digest.metadata.sample_count == 456
    assert digest.metric == "cpu-clock" and digest.unit == "samples"
    top = digest.top_self[0]
    assert top.name == "integrate_physics(int)" and top.module == "hot"
    assert top.self_pct == pytest.approx(94.52, abs=0.3)
    assert digest.hot_paths[0].frames == (
        "_start", "__libc_start_main@@GLIBC_2.34", "__libc_start_call_main", "integrate_physics(int)")
    # Entries without chains still count as self time.
    assert "finish_task_switch.isra.0" in {fn.name for fn in digest.top_self}


def test_perf_flat_children_table_and_merge():
    flat = parse_perf_flat(_text("perf_report_flat.txt"))
    by_name = {fn.name: fn for fn in flat.top_total}
    assert by_name["_start"].total_pct == pytest.approx(98.25)
    assert by_name["integrate_physics(int)"].total_pct == pytest.approx(95.61)
    assert by_name["integrate_physics(int)"].self_pct == pytest.approx(94.52)
    assert flat.top_self[0].name == "integrate_physics(int)"
    assert all(fn.self_pct > 0 for fn in flat.top_self)
    merged = merge_perf_digests(parse_perf_folded(_text("perf_report_folded.txt")), flat)
    assert merged.top_total[0].name == "_start"
    assert merged.hot_paths and merged.top_self[0].name == "integrate_physics(int)"


def test_perf_without_sample_header_uses_percent_units():
    text = ("    60.00%  app  [.] hot\n60.00% main;hot\n"
            "    40.00%  app  [.] cold\n40.00% main;cold\n")
    digest = parse_perf_folded(text)
    assert digest.unit == "0.001%"
    assert [fn.name for fn in digest.top_self] == ["hot", "cold"]
    assert digest.top_self[0].self_pct == pytest.approx(60.0)


def test_perf_plain_folded_lines_are_accepted():
    digest = parse_perf_folded("main;hot 90\nmain;cold 10\n")
    assert digest.top_self[0].name == "hot" and digest.top_self[0].self_pct == 90.0


def test_perf_garbage_is_unknown():
    with pytest.raises(ProfileFormatUnknown):
        parse_perf_folded("just some text\nwithout samples\n")
    with pytest.raises(ProfileFormatUnknown):
        parse_perf_folded("# Samples: 10  of event 'cpu-clock'\n#\n")
    with pytest.raises(ProfileFormatUnknown):
        parse_perf_flat("nothing\n")


def test_callgrind_real_fixture_top_self_and_totals():
    digest = parse_callgrind(iter(_text("callgrind.out.hot").splitlines()))
    assert digest.source_kind == "callgrind" and digest.metric == "Ir"
    assert digest.metadata.tool_version == "3.22.0"
    top = digest.top_self[0]
    assert top.name == "integrate_physics(int)" and top.self_value == 440_009
    assert top.file == "/work/spark/hot.cpp"
    # fib'2 recursion levels merge into fib: 137,953 self (callgrind_annotate: 137,933 + 20).
    fib = next(fn for fn in digest.top_self if fn.name == "fib(int)")
    assert fib.self_value == 137_953
    assert fib.total_value == 137_953  # recursion never double-counts
    main = next(fn for fn in digest.top_total if fn.name == "main")
    assert main.total_value == 595_778 and main.total_pct == 100.0
    assert digest.hot_paths[0].frames[-2:] == ("main", "integrate_physics(int)")
    assert not digest.truncated


def test_callgrind_handmade_subpositions_jumps_and_undefined_ids():
    digest = parse_callgrind(_text("callgrind.out.handmade").splitlines())
    self_values = {fn.name: fn.self_value for fn in digest.top_self}
    totals = {fn.name: fn.total_value for fn in digest.top_total}
    assert self_values == {"Physics::Integrate": 690, "fib": 200, "main": 20}
    assert totals["main"] == 910 and totals["fib"] == 200 and totals["Physics::Integrate"] == 690
    assert sum(self_values.values()) == 910
    fib = next(fn for fn in digest.top_self if fn.name == "fib")
    assert fib.calls == 5 and fib.line == 30
    assert any("undefined compressed ids" in note for note in digest.notes)
    assert [path.frames for path in digest.hot_paths] == [("main", "Physics::Integrate"), ("main", "fib")]


def test_callgrind_event_selection():
    digest = parse_callgrind(_text("callgrind.out.handmade").splitlines(), event="Dr")
    assert digest.metric == "Dr"
    assert digest.top_self[0].name == "Physics::Integrate" and digest.top_self[0].self_value == 40


def test_callgrind_line_cap_truncates():
    lines = _text("callgrind.out.hot").splitlines()
    digest = parse_callgrind(lines, CallgrindLimits(max_lines=200))
    assert digest.truncated
    assert any("budget" in note for note in digest.notes)


def test_callgrind_function_cap_collapses():
    lines = ["events: Ir", "fl=a.c"]
    for index in range(50):
        lines += ["fn=f%d" % index, "1 10"]
    digest = parse_callgrind(lines, ProfileLimits(max_functions=5))
    assert digest.truncated
    names = {fn.name for fn in digest.top_self}
    assert "[truncated]" in names and len(names) <= 6


def test_callgrind_hostile_numbers_and_lines():
    lines = ["events: Ir", "fl=a.c", "fn=main", "1 " + "9" * 10_000_000, "2 5", "+x 3",
             "fn=(77)", "3 4"]
    digest = parse_callgrind(lines)
    assert digest.top_self[0].name == "main" and digest.top_self[0].self_value == 5
    assert any("malformed" in note for note in digest.notes)


def test_callgrind_not_callgrind():
    with pytest.raises(ProfileFormatUnknown):
        parse_callgrind(["hello world", "second line"])
    with pytest.raises(ProfileFormatUnknown):
        parse_callgrind(["1 2 3"])
    with pytest.raises(ProfileParseError):
        parse_callgrind(["# callgrind format", "events: Ir"])
