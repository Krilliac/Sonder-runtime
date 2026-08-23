"""End-to-end coverage of the shipped smoke_python fixture suite.

These tests drive the real CLI (eval_harness.main) over the checked-in suite,
cassette, and baseline: real solver.solve repair loop, real grounding.run_code
subprocess grading, deterministic replayed generation. Green here means the
shipped fixtures, the runner, the baseline ratchet, and the failure report all
still agree — with no model or network anywhere.
"""
import json
import os

import eval_harness as eh

SUITE = "smoke_python"


def _read_summary(out_dir):
    path = os.path.join(out_dir, "replay", "summary.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_shipped_suite_passes_against_shipped_baseline(tmp_path):
    out_dir = str(tmp_path / "run")
    exit_code = eh.main(["run", "--suite", SUITE, "--out", out_dir,
                         "--check-baseline", "--strict"])
    assert exit_code == 0

    summary = _read_summary(out_dir)
    assert summary["schema"] == eh.RUN_SCHEMA
    statuses = {case["scenario"]: case for case in summary["cases"]}
    assert set(statuses) == {"reverse_string", "reverse_words", "slugify",
                             "sum_csv_column"}
    assert all(case["status"] == "pass" for case in summary["cases"])
    # the slugify fixture ships a buggy first response on purpose: the case
    # must pass only via the execution-grounded repair loop
    assert statuses["slugify"]["attempts"] == 2
    assert summary["totals"]["cassette_drift"] == 0
    assert len(summary["suite_hash"]) == 64
    assert len(summary["report_id"]) == 64

    # replayable trace artifacts exist and parse, ending in a trajectory record
    for scenario in statuses:
        trace_path = os.path.join(out_dir, "replay", "traces",
                                  scenario + ".jsonl")
        with open(trace_path, "r", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        assert lines[0]["schema"] == eh.TRACE_SCHEMA
        assert lines[0]["suite_hash"] == summary["suite_hash"]
        assert lines[-1]["event"] == "trajectory"
        assert lines[-1]["record"]["trajectory_digest"]

    assert os.path.exists(os.path.join(out_dir, "report.md"))
    with open(os.path.join(out_dir, "failures.json"), encoding="utf-8") as handle:
        assert json.load(handle)["failures"] == []


def test_broken_cassette_fails_baseline_with_structured_report(tmp_path):
    # drop the slugify repair recording: the repair loop's second generate
    # becomes a cassette miss, which must surface as an infra error (never a
    # graded zero) and fail the baseline
    with open(eh.default_cassette_path(SUITE), encoding="utf-8") as handle:
        cassette = json.load(handle)
    cassette["entries"]["slugify"] = cassette["entries"]["slugify"][:1]
    broken = tmp_path / "broken.cassette.json"
    broken.write_text(json.dumps(cassette), encoding="utf-8")

    out_dir = str(tmp_path / "run")
    exit_code = eh.main(["run", "--suite", SUITE, "--out", out_dir,
                         "--cassette", str(broken), "--check-baseline"])
    assert exit_code == 1

    summary = _read_summary(out_dir)
    slugify = next(case for case in summary["cases"]
                   if case["scenario"] == "slugify")
    assert slugify["status"] == "error"
    assert slugify["failure_kind"] == "cassette_miss"
    assert summary["totals"] == {
        "cases": 4, "pass": 3, "fail": 0, "error": 1, "timeout": 0,
        "graded": 3, "pass_rate": 1.0, "cassette_drift": 0}

    with open(os.path.join(out_dir, "failures.json"), encoding="utf-8") as handle:
        failures = json.load(handle)["failures"]
    assert [f["scenario"] for f in failures] == ["slugify"]
    assert failures[0]["failure"]["kind"] == "cassette_miss"
    with open(os.path.join(out_dir, "report.md"), encoding="utf-8") as handle:
        report = handle.read()
    assert "slugify" in report and "cassette_miss" in report
    assert "Baseline violations" in report


def test_tampered_baseline_fails_green_run(tmp_path):
    baseline = eh.load_baseline()
    entry = baseline["suites"][SUITE]["replay"]
    entry = dict(entry, required_pass=list(entry["required_pass"])
                 + ["phantom_scenario"])
    tampered = {"schema": eh.BASELINE_SCHEMA,
                "suites": {SUITE: {"replay": entry}}}
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(tampered), encoding="utf-8")

    exit_code = eh.main(["run", "--suite", SUITE,
                         "--out", str(tmp_path / "run"),
                         "--check-baseline", "--baseline",
                         str(baseline_path)])
    assert exit_code == 1


def test_verify_replay_cli_proves_equivalence():
    assert eh.main(["verify-replay", "--suite", SUITE]) == 0


def test_unknown_suite_is_a_harness_error_exit():
    assert eh.main(["run", "--suite", "no_such_suite"]) == 2


def test_shipped_baseline_pins_current_suite_hash():
    suite = eh.resolve_suite(SUITE)
    baseline = eh.load_baseline()
    pinned = baseline["suites"][SUITE]["replay"]["suite_hash"]
    assert pinned == suite["suite_hash"], (
        "eval_scenarios/smoke_python.json changed without re-baselining; "
        "run: python eval_harness.py run --suite smoke_python --out <dir> "
        "&& python eval_harness.py baseline update --run <dir>")
