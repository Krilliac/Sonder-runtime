"""heaptrack_print text reader.

heaptrack_print.txt is real output of the host-owned heaptrack_print template
over a heaptrack capture of tests/fixtures/profiling/leak.cpp (paths rewritten
to /work/spark).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.domain.profiling.heaptrack_text import parse_heaptrack_print, parse_size
from sonder_runtime.domain.profiling.model import ProfileFormatUnknown, ProfileParseError

FIXTURE = Path(__file__).parent / "fixtures" / "profiling" / "heaptrack_print.txt"


def _digest(**kwargs):
    return parse_heaptrack_print(FIXTURE.read_text(encoding="utf-8"), **kwargs)


def test_leak_site_and_allocator_hotspot():
    digest = _digest()
    assert digest.source_kind == "heaptrack_text" and digest.unit == "bytes"
    by_function = {hot.function: hot for hot in digest.allocations}
    leak = by_function["leak_buffer(unsigned long)"]
    assert leak.leaked_bytes == 40_960 and leak.peak_bytes == 40_960 and leak.allocations == 10
    assert leak.file == "/work/spark/src/leak.cpp" and leak.line == 7
    assert digest.allocations[0].function == "leak_buffer(unsigned long)"
    churn = by_function["churn()"]  # std::allocator plumbing frames are skipped
    assert churn.allocations == 2000 and churn.temporary == 2000 and not churn.leaked_bytes
    assert churn.line == 11


def test_footer_metadata():
    digest = _digest()
    assert digest.metadata.process == "leak"
    assert digest.metadata.sample_count == 2012
    assert digest.metadata.duration_ns == 90_000_000
    assert "total memory leaked: 45060 bytes" in digest.notes
    assert "peak heap memory consumption: 118780 bytes" in digest.notes


def test_project_predicate_picks_project_frame():
    digest = _digest(project=lambda name, module, file: bool(file and file.endswith("leak.cpp")))
    functions = {hot.function for hot in digest.allocations}
    assert "churn()" in functions and "leak_buffer(unsigned long)" in functions


def test_sizes_use_si_suffixes():
    assert parse_size("40.96", "K") == 40_960
    assert parse_size("4.85", "M") == 4_850_000
    assert parse_size("0", "") == 0


def test_not_heaptrack_and_empty():
    with pytest.raises(ProfileFormatUnknown):
        parse_heaptrack_print("random text\n")
    with pytest.raises(ProfileParseError):
        parse_heaptrack_print("MOST CALLS TO ALLOCATION FUNCTIONS\n\n")


def test_debuggee_command_line_is_reduced_to_the_program_name():
    text = ('Debuggee command was: "C:\\Users\\alice\\bin\\game.exe" --key=s3cret\n'
            "MEMORY LEAKS\n10B leaked over 1 calls from\nf()\n  at /x.cpp:1\n\n")
    digest = parse_heaptrack_print(text)
    assert digest.metadata.process == "game.exe"
