#!/usr/bin/env python3
"""Rank the suite's slowest tests from a SONDER_TEST_TIMINGS capture.

Why this exists
---------------
``--durations`` prints a ranking and throws it away with the terminal. Keeping
the per-test timings as a file makes two things possible that a scrollback
cannot do: track the same test across runs (``--compare``), and aggregate cost
by test *file*, which is the unit both a developer and pytest-xdist actually
schedule.

Capture a run, then report on it:

    SONDER_TEST_TIMINGS=timings.jsonl python -m pytest -q
    python scripts/slow_tests.py timings.jsonl
    python scripts/slow_tests.py new.jsonl --compare old.jsonl

Under pytest-xdist each worker writes ``<path>.<workerid>``; this reader globs
those back together automatically, so the same command works for both modes.

Exit codes: 0 report produced; 2 no timing records found -- an empty capture is
an infrastructure failure (wrong path? variable unset?), never "no slow tests".
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

# A phase can fail while another passes; keep the worst per test.
_OUTCOME_RANK = {"passed": 0, "skipped": 1, "failed": 2}


def load_timings(base: str) -> dict[str, dict]:
    """Aggregate phase records into one row per test nodeid."""
    paths = [p for p in [base, *sorted(glob.glob(base + ".*"))] if Path(p).is_file()]
    tests: dict[str, dict] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row = tests.setdefault(
                    record["nodeid"],
                    {"duration": 0.0, "outcome": "passed", "phases": {}},
                )
                row["duration"] += float(record.get("duration", 0.0))
                row["phases"][record.get("phase", "?")] = float(
                    record.get("duration", 0.0)
                )
                outcome = str(record.get("outcome", "passed"))
                if _OUTCOME_RANK.get(outcome, 0) > _OUTCOME_RANK.get(row["outcome"], 0):
                    row["outcome"] = outcome
    return tests


def by_file(tests: dict[str, dict]) -> dict[str, tuple[float, int]]:
    files: dict[str, list[float]] = defaultdict(list)
    for nodeid, row in tests.items():
        files[nodeid.split("::", 1)[0]].append(row["duration"])
    return {path: (sum(values), len(values)) for path, values in files.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timings", help="path passed as SONDER_TEST_TIMINGS")
    parser.add_argument("--top", type=int, default=25, help="rows per table")
    parser.add_argument("--compare", default=None,
                        help="older timings file; report per-test regressions")
    parser.add_argument("--regression-seconds", type=float, default=1.0,
                        help="minimum absolute slowdown to report")
    parser.add_argument("--regression-ratio", type=float, default=1.5,
                        help="minimum relative slowdown to report")
    arguments = parser.parse_args()

    tests = load_timings(arguments.timings)
    if not tests:
        print(
            "NO TIMING RECORDS at %r. An empty capture is an infrastructure "
            "failure (wrong path? SONDER_TEST_TIMINGS unset for the run?), "
            "not a fast suite." % arguments.timings,
            file=sys.stderr,
        )
        return 2

    total = sum(row["duration"] for row in tests.values())
    print("%d tests, %.1fs total recorded time\n" % (len(tests), total))

    print("slowest tests:")
    ranked = sorted(tests.items(), key=lambda item: -item[1]["duration"])
    for nodeid, row in ranked[: arguments.top]:
        slow_phase = max(row["phases"], key=row["phases"].get, default="?")
        print(
            "  %8.2fs  %-7s %s  [%s-heavy]"
            % (row["duration"], row["outcome"], nodeid, slow_phase)
        )

    print("\ncostliest test files:")
    files = sorted(by_file(tests).items(), key=lambda item: -item[1][0])
    for path, (seconds, count) in files[: arguments.top]:
        print("  %8.2fs  %4d tests  %s" % (seconds, count, path))

    if arguments.compare:
        old = load_timings(arguments.compare)
        if not old:
            print(
                "NO TIMING RECORDS in comparison file %r." % arguments.compare,
                file=sys.stderr,
            )
            return 2
        regressions = []
        for nodeid, row in tests.items():
            before = old.get(nodeid)
            if before is None:
                continue
            delta = row["duration"] - before["duration"]
            if delta >= arguments.regression_seconds and (
                before["duration"] <= 0
                or row["duration"] / before["duration"] >= arguments.regression_ratio
            ):
                regressions.append((delta, before["duration"], row["duration"], nodeid))
        print(
            "\nregressions vs %s (>= %.1fs and >= %.1fx):"
            % (arguments.compare, arguments.regression_seconds,
               arguments.regression_ratio)
        )
        if not regressions:
            print("  none")
        for delta, before, after, nodeid in sorted(regressions, reverse=True):
            print("  +%7.2fs  %6.2fs -> %6.2fs  %s" % (delta, before, after, nodeid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
