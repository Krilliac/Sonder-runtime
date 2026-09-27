"""Run-summary recognizers: the typed form of ``tail -1``."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.diagnostics.summary import find_summary, parse_pytest_summary


def _counts(summary):
    return (summary.tool, summary.status, summary.passed, summary.failed,
            summary.skipped, summary.errors, summary.total)


@pytest.mark.parametrize("line, expected, duration", [
    ("======= 3 failed, 10 passed, 2 skipped, 1 error, 4 warnings in 4.21s ========",
     ("pytest", "failed", 10, 3, 2, 1, 16), 4.21),
    ("3 failed, 10 passed in 0.52s", ("pytest", "failed", 10, 3, 0, 0, 13), 0.52),
    ("12 passed in 1.00s", ("pytest", "passed", 12, 0, 0, 0, 12), 1.0),
    ("===== 1 passed, 1 xfailed, 1 xpassed, 2 deselected in 0.10s =====",
     ("pytest", "passed", 2, 0, 1, 0, 3), 0.10),
    ("== 5 passed, 2 warnings in 61.02s (0:01:01) ==",
     ("pytest", "passed", 5, 0, 0, 0, 5), 61.02),
    ("2 errors in 0.30s", ("pytest", "failed", 0, 0, 0, 2, 2), 0.30),
    ("============================ no tests ran in 0.01s =============================",
     ("pytest", "unknown", 0, 0, 0, 0, 0), 0.01),
])
def test_pytest_banner_and_quiet_forms(line, expected, duration):
    summary = parse_pytest_summary(line)
    assert _counts(summary) == expected
    assert summary.duration_seconds == pytest.approx(duration)


@pytest.mark.parametrize("line", [
    "3 apples, 10 pears in 4s", "passed in 0.1s", "collected 5 items", "",
    "== test session starts ==",
])
def test_pytest_rejects_non_summaries(line):
    assert parse_pytest_summary(line) is None


def test_unittest_ok_and_failed():
    ok = ["....", "-" * 70, "Ran 4 tests in 0.002s", "", "OK (skipped=1)"]
    assert _counts(find_summary(ok)) == ("unittest", "passed", 3, 0, 1, 0, 4)
    failed = ["F.E.", "-" * 70, "Ran 4 tests in 0.010s", "", "FAILED (failures=1, errors=1)"]
    summary = find_summary(failed)
    assert _counts(summary) == ("unittest", "failed", 2, 1, 0, 1, 4)
    assert summary.duration_seconds == pytest.approx(0.010)


def test_cargo_libtest_sums_across_crates():
    lines = [
        "running 2 tests",
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s",
        "running 3 tests",
        "test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s",
        "error: test failed, to rerun pass `--lib`",
    ]
    assert _counts(find_summary(lines)) == ("cargo", "failed", 3, 1, 1, 0, 5)


def test_go_package_lines():
    passed = ["ok  \texample.com/a\t0.012s", "ok  \texample.com/b\t(cached)"]
    assert find_summary(passed).status == "passed"
    failed = ["--- FAIL: TestX (0.00s)", "FAIL", "FAIL\texample.com/a\t0.010s", "ok  \texample.com/b\t0.1s"]
    summary = find_summary(failed)
    assert summary.tool == "go" and summary.status == "failed"
    assert summary.total is None  # packages, not tests


def test_ctest_percentage_line():
    lines = [
        "1/2 Test #1: pass .............   Passed    0.00 sec",
        "50% tests passed, 1 tests failed out of 2",
        "",
        "Total Test time (real) =   0.01 sec",
        "The following tests FAILED:",
        "\t  2 - fail (Failed)",
        "Errors while running CTest",
    ]
    assert _counts(find_summary(lines)) == ("ctest", "failed", 1, 1, None, 0, 2)


def test_jest_and_vitest():
    jest = ["Test Suites: 1 failed, 1 total", "Tests:       1 failed, 4 passed, 5 total",
            "Snapshots:   0 total", "Time:        1.2 s"]
    assert _counts(find_summary(jest)) == ("jest", "failed", 4, 1, 0, 0, 5)
    vitest = [" Test Files  1 failed | 1 passed (2)", "      Tests  1 failed | 4 passed (5)",
              "   Start at  10:00:00", "   Duration  1.1s"]
    assert _counts(find_summary(vitest)) == ("vitest", "failed", 4, 1, 0, 0, 5)


def test_dotnet_maven_gradle():
    dotnet = ["Failed!  - Failed:     1, Passed:     4, Skipped:     0, Total:     5, Duration: 20 ms - app.dll (net8.0)"]
    summary = find_summary(dotnet)
    assert _counts(summary) == ("dotnet", "failed", 4, 1, 0, 0, 5)
    assert summary.duration_seconds == pytest.approx(0.02)
    maven = [
        "[ERROR] Tests run: 3, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! - in AppTest",
        "[INFO] Results:",
        "[ERROR] Tests run: 5, Failures: 1, Errors: 1, Skipped: 1",
        "[INFO] BUILD FAILURE",
    ]
    assert _counts(find_summary(maven)) == ("maven", "failed", 2, 1, 1, 1, 5)
    gradle = ["5 tests completed, 1 failed, 1 skipped", "FAILURE: Build failed with an exception."]
    assert _counts(find_summary(gradle)) == ("gradle", "failed", 3, 1, 1, 0, 5)


def test_make_error_yields_failed_without_counts():
    summary = find_summary(["cc -o t t.c", "make: *** [Makefile:3: test] Error 2"])
    assert summary.tool == "make" and summary.status == "failed"
    assert summary.total is None and summary.passed is None


def test_pytest_summary_outranks_a_trailing_make_error():
    lines = ["FAILED t.py::a", "== 1 failed, 1 passed in 0.1s ==", "make: *** [test] Error 1"]
    assert find_summary(lines).tool == "pytest"


def test_scan_goes_back_through_unrelated_trailing_lines():
    lines = ["5 passed in 0.20s"] + ["unrelated trailing line %d" % i for i in range(150)]
    assert find_summary(lines).passed == 5
    beyond = ["5 passed in 0.20s"] + ["noise %d" % i for i in range(250)]
    assert find_summary(beyond) is None
    assert find_summary(beyond, max_tail=300).passed == 5


def test_no_summary_is_none():
    assert find_summary(["hello", "world"]) is None
    assert find_summary([]) is None


def test_absurd_duration_is_dropped_so_the_wire_stays_strict_json():
    import json

    line = "= 1 passed in %ss =" % ("9" * 400)
    summary = find_summary([line])
    assert summary is not None and summary.passed == 1
    assert summary.duration_seconds is None
    json.dumps(summary.to_wire(), allow_nan=False)


@pytest.mark.parametrize("duration", ["1.2.3", ".", "..", "4."])
def test_malformed_dotnet_duration_never_raises(duration):
    line = "Passed!  - Failed: 0, Passed: 4, Skipped: 0, Total: 4, Duration: %s s" % duration
    summary = find_summary([line])
    assert summary is not None and summary.tool == "dotnet" and summary.passed == 4
    assert summary.duration_seconds is None
