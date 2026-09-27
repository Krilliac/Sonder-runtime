"""Tracy csvexport, WPA, PIX and Superluminal CSV readers.

The WPA/PIX/Superluminal fixtures use header names from vendor documentation
and are flagged for replacement with real exports during Windows live
validation (the digests carry an "unverified export" note until then).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.domain.profiling.model import (
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
)
from sonder_runtime.domain.profiling.tabular_csv import parse_profile_csv, sniff_profile_csv
from sonder_runtime.domain.profiling.tracy_csv import VERSION_HINT, parse_tracy_csv

FIXTURES = Path(__file__).parent / "fixtures" / "profiling"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_tracy_aggregate_zone_totals_and_spikes():
    digest = parse_tracy_csv(_text("tracy_aggregate.csv"))
    assert digest.source_kind == "tracy_csv" and digest.unit == "ns"
    assert [fn.name for fn in digest.top_total][:3] == ["Frame", "Physics::Integrate", "Render"]
    physics = digest.top_total[1]
    assert physics.total_value == 960_000_000 and physics.calls == 120
    assert physics.total_pct == 48.0 and physics.file == "/work/spark/src/physics.cpp"
    assert physics.line == 88
    assert digest.top_self == ()
    assert {spike.label for spike in digest.spikes} == {"Physics::Integrate"}
    assert "Audio::Mix, stereo" in {fn.name for fn in digest.top_total}  # quoted comma
    assert any("inclusive zone times only" in note for note in digest.notes)


def test_tracy_unwrap_frames_self_time_and_spikes():
    digest = parse_tracy_csv(_text("tracy_unwrap.csv"), frame_zone="Frame", frame_budget_ms=16.6)
    assert digest.frames is not None and digest.frames.count == 40
    assert digest.frames.max_ms == pytest.approx(40.0)
    assert sorted(spike.duration_ns for spike in digest.spikes) == [40_000_000, 40_000_000]
    assert digest.top_self[0].name == "Physics::Integrate"
    assert digest.hot_paths[0].frames == ("Frame", "Update", "Physics::Integrate")
    assert digest.metadata.threads == 2


def test_tracy_unwrap_thread_filter():
    digest = parse_tracy_csv(_text("tracy_unwrap.csv"), thread="2")
    assert [fn.name for fn in digest.top_self] == ["Job::Decode"]
    assert digest.frames is None


def test_tracy_version_mismatch_is_unknown_with_hint():
    text = ("The file you are trying to open is not supported.\n"
            "It was saved by a newer version of Tracy (0.11.1).\n")
    with pytest.raises(ProfileFormatUnknown) as caught:
        parse_tracy_csv(text)
    assert caught.value.hint == VERSION_HINT[:240]
    with pytest.raises(ProfileFormatUnknown):
        parse_tracy_csv("a,b,c\n1,2,3\n")


def test_dispatch_by_header():
    assert sniff_profile_csv(_text("tracy_aggregate.csv").splitlines()[0].split(",")) == "tracy_csv"
    assert parse_profile_csv(_text("tracy_unwrap.csv")).source_kind == "tracy_csv"
    assert parse_profile_csv(_text("wpa_cpu_sampled.csv")).source_kind == "wpa_csv"
    assert parse_profile_csv(_text("pix_timing.csv")).source_kind == "pix_csv"
    assert parse_profile_csv(_text("superluminal_functions.csv")).source_kind == "superluminal_csv"
    with pytest.raises(ProfileFormatUnknown):
        parse_profile_csv("colour,flavour\nred,sweet\n")
    with pytest.raises(ProfileFormatUnknown):
        parse_profile_csv(_text("pix_timing.csv"), "wpa")


def test_wpa_sampled_stacks_with_thousands_separators():
    digest = parse_profile_csv(_text("wpa_cpu_sampled.csv"))
    top = digest.top_self[0]
    assert top.name == "spark.exe!Physics::Integrate" and top.module == "spark.exe"
    assert top.self_value == 5_120 * 1_000_000 and top.self_pct == pytest.approx(61.2, abs=0.01)
    totals = {fn.name: fn.total_value for fn in digest.top_total}
    assert totals["spark.exe!Game::Tick"] == (5_120 + 2_048) * 1_000_000
    assert digest.hot_paths[0].frames[-1] == "spark.exe!Physics::Integrate"
    assert any("unverified export" in note for note in digest.notes)


def test_pix_timing_frames_and_nesting():
    digest = parse_profile_csv(_text("pix_timing.csv"), frame_budget_ms=16.6)
    assert digest.frames is not None and digest.frames.count == 30
    assert digest.frames.over_budget == 1
    assert [spike.duration_ns for spike in digest.spikes] == [40_000_000]
    assert digest.spikes[0].thread == "Render Thread"
    assert digest.top_total[0].name == "Frame"
    assert digest.hot_paths[0].frames == ("Frame", "DrawScene")


def test_pix_without_start_column_notes_missing_nesting():
    digest = parse_profile_csv("Name,Duration (us)\nDraw,1500\nDraw,1500\nBlit,200\n")
    assert digest.top_self[0].name == "Draw" and digest.top_self[0].self_value == 3_000_000
    assert any("no start column" in note for note in digest.notes)


def test_superluminal_inclusive_exclusive():
    digest = parse_profile_csv(_text("superluminal_functions.csv"))
    assert digest.top_self[0].name == "Physics::Integrate"
    assert digest.top_self[0].self_value == 600_250_000
    assert digest.top_total[0].name == "Game::Tick"
    assert digest.top_total[0].total_value == 980_000_000
    memcpy = next(fn for fn in digest.top_self if fn.name == "memcpy")
    assert memcpy.module == "vcruntime140.dll" and memcpy.calls == 5000


def test_oversize_csv_line_is_skipped_and_counted():
    huge = "Physics," + "9" * (64 * 1024 * 1024) + ",1\n"
    text = "Name,Start (ms),Duration (ms)\n" + huge + "Draw,0,2\n"
    digest = parse_profile_csv(text)
    assert [fn.name for fn in digest.top_self] == ["Draw"]
    assert any("over 64 KiB" in note for note in digest.notes)


def test_giant_number_cell_is_rejected_not_parsed():
    text = "Name,Start (ms),Duration (ms)\nX,0," + "1" * 60_000 + "\nY,0,2\n"
    digest = parse_profile_csv(text)
    assert [fn.name for fn in digest.top_self] == ["Y"]


def test_rows_cap_truncates():
    rows = "".join("Z,%d,1\n" % i for i in range(500))
    digest = parse_profile_csv("Name,Start (ms),Duration (ms)\n" + rows,
                               limits=ProfileLimits(max_lines=100))
    assert digest.truncated


def test_header_only_is_parse_error():
    with pytest.raises(ProfileParseError):
        parse_profile_csv("Name,Start (ms),Duration (ms)\n")
    with pytest.raises(ProfileFormatUnknown):
        parse_profile_csv("")
