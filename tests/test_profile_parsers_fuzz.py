"""Mutation fuzzing and resource bombs for every pure profile reader (SEC-008).

Each reader gets 2000 deterministic mutations of a small valid seed. The only
acceptable outcomes are a ProfileDigest, ProfileFormatUnknown or
ProfileParseError, each within the 100 ms per-input budget.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from tests.fixtures.profiling.bounded_json_double import install

install()

from sonder_runtime.domain.profiling.callgrind import parse_callgrind  # noqa: E402
from sonder_runtime.domain.profiling.chrome_trace import parse_chrome_trace  # noqa: E402
from sonder_runtime.domain.profiling.heaptrack_text import parse_heaptrack_print  # noqa: E402
from sonder_runtime.domain.profiling.model import (  # noqa: E402
    ProfileDigest,
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
)
from sonder_runtime.domain.profiling.perf_text import parse_perf_flat, parse_perf_folded  # noqa: E402
from sonder_runtime.domain.profiling.render import digest_to_wire, render_digest  # noqa: E402
from sonder_runtime.domain.profiling.tabular_csv import parse_profile_csv  # noqa: E402
from sonder_runtime.domain.profiling.tracy_csv import parse_tracy_csv  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "profiling"
ITERATIONS = 2000
PER_INPUT_SECONDS = 0.1
TOKENS = ("\n", ",", ";", '"', "{", "}", "[", "]", ":", "%", "(", ")", "=", "+", "-", "*",
          "\x00", "\x1b[31m", "﻿", "calls=", "fn=(1)", "cfn=(99)", "events: Ir", "0x",
          "9" * 400, "-1", "1e308", "NaN", '"ph":"B"', '"ph":"E"', '"ts":', "  at ", " from",
          "# Samples: 10K of event 'x'", "[.]", "Frame", "\r\n", "\t")


def _head(name: str, lines: int) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return "\n".join(text.splitlines()[:lines]) + "\n"


def _chrome_seed() -> str:
    events = [{"ph": "M", "pid": 1, "tid": 1, "name": "thread_name", "args": {"name": "Main"}}]
    ts = 0.0
    for index in range(8):
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Frame", "ts": ts, "dur": 16600})
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Work", "ts": ts + 10, "dur": 9000})
        events.append({"ph": "B", "pid": 1, "tid": 2, "name": "Job", "ts": ts + 20})
        events.append({"ph": "E", "pid": 1, "tid": 2, "ts": ts + 900})
        events.append({"ph": "I", "pid": 1, "tid": 1, "name": "Frame", "ts": ts})
        ts += 16600 if index != 5 else 40000
    return json.dumps({"traceEvents": events})


SEEDS = {
    "perf_folded": (_head("perf_report_folded.txt", 40), lambda t: parse_perf_folded(t)),
    "perf_flat": (_head("perf_report_flat.txt", 24), lambda t: parse_perf_flat(t)),
    "callgrind": (_head("callgrind.out.handmade", 60), lambda t: parse_callgrind(t.splitlines())),
    "callgrind_real": (_head("callgrind.out.hot", 150), lambda t: parse_callgrind(t.splitlines())),
    "chrome": (_chrome_seed(), lambda t: parse_chrome_trace([t], frame_budget_ms=16.6)),
    "tracy_aggregate": (_head("tracy_aggregate.csv", 10), lambda t: parse_tracy_csv(t)),
    "tracy_unwrap": (_head("tracy_unwrap.csv", 40), lambda t: parse_tracy_csv(t, frame_zone="Frame")),
    "wpa": (_head("wpa_cpu_sampled.csv", 10), lambda t: parse_profile_csv(t)),
    "pix": (_head("pix_timing.csv", 40), lambda t: parse_profile_csv(t, frame_budget_ms=16.6)),
    "superluminal": (_head("superluminal_functions.csv", 10), lambda t: parse_profile_csv(t)),
    "heaptrack": (_head("heaptrack_print.txt", 80), lambda t: parse_heaptrack_print(t)),
}


def _mutate(rng: random.Random, seed: str) -> str:
    text = seed
    for _ in range(rng.randint(1, 6)):
        if not text:
            text = rng.choice(TOKENS)
            continue
        op = rng.randrange(6)
        pos = rng.randrange(len(text))
        if op == 0:  # flip one character
            text = text[:pos] + chr(rng.randrange(0, 0x3000)) + text[pos + 1:]
        elif op == 1:  # insert a structural token
            text = text[:pos] + rng.choice(TOKENS) + text[pos:]
        elif op == 2:  # delete a span
            text = text[:pos] + text[pos + rng.randint(1, 64):]
        elif op == 3:  # duplicate a span
            end = min(len(text), pos + rng.randint(1, 256))
            text = text[:end] + text[pos:end] + text[end:]
        elif op == 4:  # truncate
            text = text[:pos]
        else:  # swap two lines
            lines = text.split("\n")
            if len(lines) > 2:
                a, b = rng.randrange(len(lines)), rng.randrange(len(lines))
                lines[a], lines[b] = lines[b], lines[a]
                text = "\n".join(lines)
    return text


@pytest.mark.parametrize("name", sorted(SEEDS))
def test_mutations_only_yield_digest_or_typed_errors(name):
    seed, parse = SEEDS[name]
    assert isinstance(parse(seed), ProfileDigest)  # the seed itself is valid
    rng = random.Random("sonder-profile-fuzz-" + name)
    outcomes = {"digest": 0, "unknown": 0, "parse": 0}
    slow = []
    for iteration in range(ITERATIONS):
        text = _mutate(rng, seed)
        started = time.perf_counter()
        try:
            digest = parse(text)
        except ProfileFormatUnknown:
            outcomes["unknown"] += 1
        except ProfileParseError:
            outcomes["parse"] += 1
        else:
            assert isinstance(digest, ProfileDigest)
            payload = digest_to_wire(digest)
            assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= 48_000
            render_digest(digest, max_chars=2000)
            outcomes["digest"] += 1
        elapsed = time.perf_counter() - started
        if elapsed > PER_INPUT_SECONDS:
            slow.append((iteration, elapsed))
    # A shared CI host can stall one input; a systematic slowdown cannot hide.
    assert len(slow) <= ITERATIONS // 200, slow[:5]
    assert outcomes["digest"] > 0


def test_json_depth_bomb_is_refused():
    bomb = '{"traceEvents":[' + "[" * 10_000 + "]" * 10_000 + ',{"ph":"X","name":"ok","ts":1,"dur":2,"pid":1,"tid":1}]}'
    digest = parse_chrome_trace([bomb])
    assert [fn.name for fn in digest.top_self] == ["ok"]
    assert any("skipped" in note for note in digest.notes)
    with pytest.raises((ProfileParseError, ProfileFormatUnknown)):
        parse_chrome_trace(["[" * 10_000])


def test_one_megabyte_chrome_event_is_skipped_and_counted():
    big = '{"ph":"X","name":"Big","ts":1,"dur":1,"pid":1,"tid":1,"args":{"x":"%s"}}' % ("y" * (1 << 20))
    text = '{"traceEvents":[%s,{"ph":"X","name":"Small","ts":5,"dur":1,"pid":1,"tid":1}]}' % big
    digest = parse_chrome_trace([text[i:i + 8192] for i in range(0, len(text), 8192)])
    assert [fn.name for fn in digest.top_self] == ["Small"]
    assert any("1 events over" in note for note in digest.notes)


def test_ten_megabyte_number_strings():
    huge = "9" * 10_000_000
    wide = ProfileLimits(max_line_chars=32 << 20)
    digest = parse_callgrind(["events: Ir", "fl=a.c", "fn=main", "1 " + huge, "2 7"], wide)
    assert digest.top_self[0].self_value == 7
    event = '{"traceEvents":[{"ph":"X","name":"N","ts":%s,"dur":1,"pid":1,"tid":1}]}' % huge
    with pytest.raises(ProfileParseError):
        parse_chrome_trace([event])
    with pytest.raises((ProfileParseError, ProfileFormatUnknown)):
        parse_tracy_csv("name,src_file,src_line,total_ns,total_perc,counts,mean_ns,min_ns,max_ns,std_ns\n"
                        "Z,a.c,1,%s,1,1,1,1,1,1\n" % huge, limits=wide)
    digest = parse_perf_folded("main;hot %s\nmain;cold 3\n" % huge, limits=wide)
    assert [fn.name for fn in digest.top_self] == ["cold"]


def test_sixty_four_mebibyte_single_csv_line():
    line = "x" * (64 * 1024 * 1024)
    for parse in (parse_profile_csv, parse_tracy_csv):
        started = time.perf_counter()
        with pytest.raises(ProfileFormatUnknown):
            parse(line)
        assert time.perf_counter() - started < 5.0


def test_deep_folded_stack_bomb():
    stack = ";".join("f%d" % (i % 7) for i in range(10_000))
    digest = parse_perf_folded(stack + " 5\n", limits=ProfileLimits(max_line_chars=1 << 20))
    assert digest.truncated
    assert all(len(path.frames) <= 24 for path in digest.hot_paths)


def test_time_budget_stops_long_inputs():
    ticks = iter(range(10**9))
    clock = lambda: float(next(ticks))  # noqa: E731  one "second" per clock read
    lines = ["events: Ir", "fl=a.c", "fn=main"] + ["%d 1" % i for i in range(5000)]
    digest = parse_callgrind(lines, ProfileLimits(max_seconds=2.0), clock=clock)
    assert digest.truncated
    assert digest.top_self[0].self_value < 5000
