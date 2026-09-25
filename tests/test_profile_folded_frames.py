"""Folded stacks (self/total, recursion, bounds, hot paths) and frame statistics."""
from __future__ import annotations

import math

import pytest

from sonder_runtime.domain.profiling.folded import (
    FoldedProfile,
    FrameInfo,
    digest_folded,
    fold_add,
    fold_zones,
    function_values,
    parse_folded_text,
)
from sonder_runtime.domain.profiling.frames import (
    detect_spikes,
    frame_stats,
    nearest_rank,
    spike_threshold,
)
from sonder_runtime.domain.profiling.model import (
    ELISION,
    TRUNCATED_FRAME,
    ProfileFormatUnknown,
    ProfileLimits,
)

MS = 1_000_000


def _digest(folded, **kwargs):
    return digest_folded(folded, metric="samples", unit="samples", source_kind="perf_text",
                         **kwargs)


def test_self_and_total_are_consistent():
    folded = FoldedProfile()
    fold_add(folded, ["main", "tick", "physics"], 60)
    fold_add(folded, ["main", "tick", "render"], 30)
    fold_add(folded, ["main"], 10)
    digest = _digest(folded)
    self_values = {fn.name: fn.self_value for fn in digest.top_self}
    totals = {fn.name: fn.total_value for fn in digest.top_total}
    assert sum(self_values.values()) == folded.total == 100
    assert totals == {"main": 100, "tick": 90, "physics": 60, "render": 30}
    assert self_values == {"physics": 60, "render": 30, "main": 10}
    top = digest.top_self[0]
    assert top.name == "physics" and top.self_pct == 60.0 and top.total_pct == 60.0


def test_recursion_is_counted_once_per_stack():
    folded = FoldedProfile()
    fold_add(folded, ["main", "fib", "fib", "fib", "fib"], 40)
    fold_add(folded, ["main", "fib", "fib"], 10)
    fold_add(folded, ["main", "other"], 50)
    self_values, totals = function_values(folded)
    assert totals["fib"] == 50  # not 40*4 + 10*2
    assert totals["main"] == 100
    digest = _digest(folded)
    fib = next(fn for fn in digest.top_total if fn.name == "fib")
    assert fib.total_pct == 50.0 and fib.total_value <= folded.total


def test_hot_paths_are_ordered_and_merged_at_common_prefix():
    folded = FoldedProfile()
    fold_add(folded, ["main", "a", "x"], 45)
    fold_add(folded, ["main", "b", "y"], 40)
    fold_add(folded, ["main", "c"], 15)
    # Weight spread thinly under "spread" merges into the prefix.
    for i in range(30):
        fold_add(folded, ["root2", "spread", "leaf%d" % i], 1)
    digest = _digest(folded)
    frames = [path.frames for path in digest.hot_paths]
    values = [path.value for path in digest.hot_paths]
    assert frames[:3] == [("main", "a", "x"), ("main", "b", "y"), ("root2", "spread")]
    assert values == sorted(values, reverse=True)
    assert ("main", "c") in frames


def test_hot_path_elides_long_chains_to_24_frames():
    folded = FoldedProfile()
    fold_add(folded, ["f%d" % i for i in range(60)], 10)
    path = _digest(folded).hot_paths[0]
    assert len(path.frames) == 24 and ELISION in path.frames
    assert path.frames[0] == "f0" and path.frames[-1] == "f59"


def test_depth_and_stack_count_truncation():
    folded = FoldedProfile(max_stacks=3, max_depth=128)
    fold_add(folded, ["f%d" % i for i in range(10_000)], 5)
    stack = next(iter(folded.stacks))
    assert len(stack) == 128 and TRUNCATED_FRAME in stack and stack[-1] == "f9999"
    for i in range(10):
        fold_add(folded, ["main", "g%d" % i], 1)
    assert folded.truncated and len(folded.stacks) == 4  # 3 + the [truncated] bucket
    assert folded.stacks[(TRUNCATED_FRAME,)] == 8
    digest = _digest(folded)
    assert digest.truncated
    assert all(fn.name != TRUNCATED_FRAME for fn in digest.top_self)
    assert any("collapsed" in note for note in digest.notes)
    assert any("[truncated]" in note for note in digest.notes)


def test_in_project_predicate_uses_frame_info():
    folded = FoldedProfile()
    fold_add(folded, ["main", "Game::Tick"], 5, info={"Game::Tick": FrameInfo(file="src/game.cpp")})
    fold_add(folded, ["main", "memcpy"], 5, info={"memcpy": FrameInfo(module="libc.so.6")})
    digest = _digest(folded, project=lambda name, module, file: bool(file and file.startswith("src/")))
    marks = {fn.name: fn.in_project for fn in digest.top_self}
    assert marks == {"Game::Tick": True, "memcpy": False}


def test_parse_folded_text_and_unknown():
    folded = parse_folded_text("# comment\nmain;a 3\nmain;b 2\nmain;a 1\nbad line\n")
    assert folded.stacks == {("main", "a"): 4, ("main", "b"): 2}
    assert any("malformed" in note for note in folded.notes)
    with pytest.raises(ProfileFormatUnknown):
        parse_folded_text("no numbers here\n")


def test_fold_add_ignores_non_positive_and_garbage_values():
    folded = FoldedProfile()
    fold_add(folded, ["a"], 0)
    fold_add(folded, ["a"], -3)
    fold_add(folded, ["a"], "nan")  # type: ignore[arg-type]
    assert folded.total == 0 and not folded.stacks


def test_fold_zones_nests_by_containment_and_clips_overruns():
    folded = FoldedProfile()
    zones = [
        (0, 100, "Frame"),
        (10, 50, "Update"),
        (20, 20, "Physics"),
        (70, 40, "Overrun"),  # ends at 110 > parent end 100 -> clipped to 30
        (200, 10, "Next"),
    ]
    assert fold_zones(folded, list(reversed(zones))) == 5
    assert folded.stacks == {
        ("Frame", "Update", "Physics"): 20,
        ("Frame", "Update"): 30,
        ("Frame", "Overrun"): 30,
        ("Frame",): 20,
        ("Next",): 10,
    }


def test_fold_zones_deep_nesting_is_iterative():
    folded = FoldedProfile()
    zones = [(i, 100_000 - 2 * i, "z%d" % (i % 3)) for i in range(20_000)]
    fold_zones(folded, zones)
    assert folded.truncated and folded.depth_clipped > 0


def test_frame_stats_nearest_rank_is_exact():
    series = [float(i) * MS for i in range(1, 101)]  # 1..100 ms
    stats = frame_stats(series, budget_ms=90)
    assert stats.count == 100
    assert stats.p50_ms == 50.0 and stats.p95_ms == 95.0 and stats.p99_ms == 99.0
    assert stats.max_ms == 100.0 and stats.over_budget == 10
    small = frame_stats([10 * MS, 20 * MS, 30 * MS])
    assert small.p50_ms == 20.0 and small.p95_ms == 30.0 and small.p99_ms == 30.0
    assert nearest_rank([1, 2, 3, 4], 25) == 1
    assert frame_stats([]) is None
    assert frame_stats([float("nan"), -1]) is None
    assert frame_stats([MS], budget_ms=float("inf")).budget_ms is None


def _game_series():
    series = [16.6 + ((i * 37) % 7 - 3) * 0.1 for i in range(120)]
    for index in (30, 60, 90):
        series[index] = 40.0
    series[45] = 18.0
    return [value * MS for value in series]


def test_spikes_flag_40ms_but_not_18ms_mad_floor_regression():
    series = _game_series()
    spikes = detect_spikes(series, starts_ns=list(range(120)))
    assert sorted(spike.start_ns for spike in spikes) == [30, 60, 90]
    assert all(spike.duration_ns == 40 * MS for spike in spikes)
    assert all(math.isclose(spike.ratio_to_median, 40.0 / 16.6, rel_tol=0.01) for spike in spikes)
    median, threshold = spike_threshold(series)
    assert 18 * MS < threshold < 40 * MS


def test_flat_series_has_no_spikes():
    assert detect_spikes([16.6 * MS] * 500) == ()
    assert detect_spikes([0.1 * MS] * 50 + [0.4 * MS]) == ()  # below the 0.5 ms floor
    assert detect_spikes([1, 2]) == ()


def test_spikes_are_capped_and_ordered_by_ratio():
    series = [10 * MS] * 200 + [30 * MS + i * MS for i in range(20)]
    spikes = detect_spikes(series, max_spikes=5, labels=["z%d" % i for i in range(220)])
    assert len(spikes) == 5
    assert [spike.label for spike in spikes] == ["z219", "z218", "z217", "z216", "z215"]


def test_limits_are_the_spec_values():
    limits = ProfileLimits()
    assert (limits.max_stacks, limits.max_depth) == (200_000, 128)
    assert (limits.max_lines, limits.max_functions) == (2_000_000, 100_000)
    assert (limits.max_events, limits.max_event_bytes, limits.max_line_chars) == (2_000_000, 65_536, 65_536)
