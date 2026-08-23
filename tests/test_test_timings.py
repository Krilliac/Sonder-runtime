"""Per-test timing capture (conftest) and its reader (scripts/slow_tests.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest as root_conftest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "slow_tests.py"
_spec = importlib.util.spec_from_file_location("slow_tests", _MODULE_PATH)
slow_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slow_tests)


@pytest.fixture()
def _clean_records(monkeypatch):
    # sessionfinish also runs the hermetic state-root cleanup; invoking that
    # for real here would delete the live session's SONDER_HOME out from under
    # every later test.
    monkeypatch.setattr(root_conftest, "_cleanup_test_state", lambda: None)
    # When the surrounding pytest run itself captures timings, the live hook
    # records this test's own phases into the shared list and the counts below
    # go wrong. Neutralize the ambient capture; each test sets its own.
    monkeypatch.delenv("SONDER_TEST_TIMINGS", raising=False)
    saved = list(root_conftest._timing_records)
    root_conftest._timing_records.clear()
    yield root_conftest._timing_records
    root_conftest._timing_records[:] = saved


def _report(nodeid, phase, outcome, duration):
    return SimpleNamespace(nodeid=nodeid, when=phase, outcome=outcome, duration=duration)


def test_capture_is_off_without_the_environment_variable(monkeypatch, _clean_records):
    monkeypatch.delenv("SONDER_TEST_TIMINGS", raising=False)
    root_conftest.pytest_runtest_logreport(_report("t::a", "call", "passed", 1.0))
    assert _clean_records == []


def test_capture_writes_one_json_line_per_phase(monkeypatch, tmp_path, _clean_records):
    out = tmp_path / "timings.jsonl"
    monkeypatch.setenv("SONDER_TEST_TIMINGS", str(out))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    for phase, duration in (("setup", 0.5), ("call", 2.0), ("teardown", 0.1)):
        root_conftest.pytest_runtest_logreport(_report("t::a", phase, "passed", duration))
    root_conftest.pytest_sessionfinish(None, 0)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["phase"] for line in lines] == ["setup", "call", "teardown"]
    assert all(line["nodeid"] == "t::a" for line in lines)


def test_xdist_workers_write_suffixed_files(monkeypatch, tmp_path, _clean_records):
    out = tmp_path / "timings.jsonl"
    monkeypatch.setenv("SONDER_TEST_TIMINGS", str(out))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    root_conftest.pytest_runtest_logreport(_report("t::a", "call", "passed", 1.0))
    root_conftest.pytest_sessionfinish(None, 0)
    assert not out.exists()
    assert (tmp_path / "timings.jsonl.gw3").exists()


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_reader_merges_worker_files_and_sums_phases(tmp_path):
    base = tmp_path / "timings.jsonl"
    _write(tmp_path / "timings.jsonl.gw0", [
        {"nodeid": "tests/a.py::t1", "phase": "setup", "outcome": "passed", "duration": 0.5},
        {"nodeid": "tests/a.py::t1", "phase": "call", "outcome": "failed", "duration": 2.0},
    ])
    _write(tmp_path / "timings.jsonl.gw1", [
        {"nodeid": "tests/b.py::t2", "phase": "call", "outcome": "passed", "duration": 4.0},
    ])
    tests = slow_tests.load_timings(str(base))
    assert tests["tests/a.py::t1"]["duration"] == pytest.approx(2.5)
    assert tests["tests/a.py::t1"]["outcome"] == "failed"
    files = slow_tests.by_file(tests)
    assert files["tests/b.py"] == (pytest.approx(4.0), 1)


def test_reader_exits_2_on_an_empty_capture(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["slow_tests.py", str(tmp_path / "missing.jsonl")]
    )
    assert slow_tests.main() == 2
    assert "infrastructure failure" in capsys.readouterr().err


def test_reader_reports_regressions_against_an_older_run(tmp_path, capsys, monkeypatch):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    _write(old, [
        {"nodeid": "tests/a.py::t1", "phase": "call", "outcome": "passed", "duration": 1.0},
        {"nodeid": "tests/a.py::t2", "phase": "call", "outcome": "passed", "duration": 1.0},
    ])
    _write(new, [
        {"nodeid": "tests/a.py::t1", "phase": "call", "outcome": "passed", "duration": 4.0},
        {"nodeid": "tests/a.py::t2", "phase": "call", "outcome": "passed", "duration": 1.1},
    ])
    monkeypatch.setattr(
        "sys.argv",
        ["slow_tests.py", str(new), "--compare", str(old)],
    )
    assert slow_tests.main() == 0
    out = capsys.readouterr().out
    assert "tests/a.py::t1" in out.split("regressions")[1]
    assert "t2" not in out.split("regressions")[1]
