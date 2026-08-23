#!/usr/bin/env python3
"""Run pytest with bounded concurrency and emit privacy-safe timing evidence.

The report stores only test node IDs, outcomes, durations, counts, and resource
limits.  It never captures stdout, exception text, environment variables, test
parameters beyond their existing node IDs, or source contents.

Examples::

    python scripts/profile_tests.py tests/test_admission_fairness.py
    python scripts/profile_tests.py --since main --workers 2
    python scripts/profile_tests.py --top 50 --output .pytest_cache/profile.json

Serial execution is the default.  Parallel execution is opt-in, uses xdist's
``loadfile`` scheduler to preserve file-level grouping, and is capped at four
workers to avoid turning a test speedup into workstation memory pressure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


SCHEMA = "sonder.pytest-profile.v1"
MAX_WORKERS = 4
DEFAULT_OUTPUT = Path(".pytest_cache/sonder-test-profile.json")


class TimingRecorder:
    """Small pytest plugin retaining only a bounded slow-test leaderboard."""

    def __init__(self, top: int) -> None:
        self.top = top
        self.records: dict[str, dict[str, object]] = {}
        self.collected = 0

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report) -> None:
        record = self.records.setdefault(report.nodeid, {
            "nodeid": report.nodeid,
            "outcome": "passed",
            "duration_seconds": 0.0,
        })
        record["duration_seconds"] = float(record["duration_seconds"]) + max(
            0.0, float(report.duration)
        )
        if report.failed:
            record["outcome"] = "failed"
        elif report.skipped and record["outcome"] != "failed":
            record["outcome"] = "skipped"

    def summary(self) -> tuple[dict[str, int], list[dict[str, object]]]:
        outcomes = {"passed": 0, "failed": 0, "skipped": 0}
        for record in self.records.values():
            outcomes[str(record["outcome"])] += 1
        slowest = sorted(
            self.records.values(),
            key=lambda row: (-float(row["duration_seconds"]), str(row["nodeid"])),
        )[: self.top]
        return outcomes, [
            {
                "nodeid": row["nodeid"],
                "outcome": row["outcome"],
                "duration_seconds": round(float(row["duration_seconds"]), 6),
            }
            for row in slowest
        ]


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _incremental_selection(repo: Path, since: str) -> dict[str, object]:
    selector = Path(__file__).with_name("select_regression_tests.py")
    result = subprocess.run(
        [
            sys.executable, str(selector), "--repo", str(repo),
            "--since", since, "--format", "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "incremental selection failed closed: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    payload = json.loads(result.stdout)
    if not payload.get("selected"):
        raise RuntimeError("incremental selection returned no tests")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="pytest paths or node IDs")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument(
        "--since",
        help="select tests from this Git base; cannot be combined with paths",
    )
    parser.add_argument(
        "--workers", type=int, default=0,
        help="xdist workers (0 is serial; maximum 4)",
    )
    parser.add_argument("--top", type=int, default=25, help="slow tests retained (1-200)")
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT), help="atomic JSON report path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args, pytest_extra = _parser().parse_known_args(argv)
    if not 0 <= args.workers <= MAX_WORKERS:
        raise SystemExit("--workers must be between 0 and %d" % MAX_WORKERS)
    if not 1 <= args.top <= 200:
        raise SystemExit("--top must be between 1 and 200")
    if args.since and args.paths:
        raise SystemExit("--since cannot be combined with explicit test paths")

    repo = Path(args.repo).resolve()
    if not (repo / "pytest.ini").is_file():
        raise SystemExit("repository has no pytest.ini: %s" % repo)
    output = Path(args.output)
    if not output.is_absolute():
        output = repo / output

    selection = None
    paths = list(args.paths)
    if args.since:
        try:
            selection = _incremental_selection(repo, args.since)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(str(exc)) from exc
        paths = list(selection["selected"])

    pytest_args = ["-q", *paths, *pytest_extra]
    parallelism = "serial"
    if args.workers:
        pytest_args.extend(["-n", str(args.workers), "--dist", "loadfile"])
        parallelism = "xdist-loadfile"

    recorder = TimingRecorder(args.top)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    previous_cwd = Path.cwd()
    try:
        os.chdir(repo)
        exit_code = int(pytest.main(pytest_args, plugins=[recorder]))
    finally:
        os.chdir(previous_cwd)
    wall_seconds = time.perf_counter() - wall_started
    controller_cpu_seconds = time.process_time() - cpu_started
    outcomes, slowest = recorder.summary()

    report: dict[str, object] = {
        "schema": SCHEMA,
        "exit_code": exit_code,
        "wall_seconds": round(wall_seconds, 6),
        "controller_cpu_seconds": round(controller_cpu_seconds, 6),
        "logical_cpu_count": os.cpu_count(),
        "parallelism": parallelism,
        "workers": args.workers,
        "collected_tests": recorder.collected or len(recorder.records),
        "reported_tests": len(recorder.records),
        "outcomes": outcomes,
        "slowest": slowest,
        "slowest_limit": args.top,
        "selection": None if selection is None else {
            "schema": selection.get("schema"),
            "selected_count": selection.get("selected_count"),
            "test_file_count": selection.get("test_file_count"),
            "changed_modules": selection.get("changed_modules"),
            "uncovered_identifiers": selection.get("uncovered_identifiers"),
        },
    }
    _atomic_json(output, report)
    print(
        "profile report: %s (%d tests, %.3fs, %s)"
        % (output, len(recorder.records), wall_seconds, parallelism)
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
