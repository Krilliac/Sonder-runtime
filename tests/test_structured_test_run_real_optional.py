"""Real cargo and go runs (offline, no dependencies), skipped per tool."""
from __future__ import annotations

import shutil
import time

import pytest

from sonder_runtime.application.testing.ports import TestRunRequest
from sonder_runtime.domain.testing.report import TestReport
from tests.test_tools_test_runs_harness import stack  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def _finish(stack, job_id, limit=240.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = stack.service.result(job_id, stack.context(), wait_seconds=10)
        if isinstance(result, TestReport):
            return result
    raise AssertionError("run did not finish")


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_cargo_libtest_totals(stack, monkeypatch):
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    crate = stack.allowed / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "crate1"\nversion = "0.1.0"\nedition = "2021"\n')
    (crate / "src" / "lib.rs").write_text(
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n"
        "    #[test]\n    fn it_works() { assert_eq!(add(2, 2), 4); }\n"
        "    #[test]\n    fn it_fails() { assert_eq!(add(2, 2), 5); }\n}\n")
    report = _finish(stack, stack.service.start(TestRunRequest(project=str(crate)), stack.context()))
    assert report.runner == "cargo" and report.totals_source == "libtest_text"
    assert (report.totals.passed, report.totals.failed) == (1, 1)
    assert [item.id for item in report.failures] == ["tests::it_fails"]
    assert report.failures[0].file == "src/lib.rs"
    assert report.status == "failed"


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_go_json_totals(stack, monkeypatch, tmp_path):
    monkeypatch.setenv("GOFLAGS", "-mod=mod")
    monkeypatch.setenv("GOPROXY", "off")
    module = stack.allowed / "gomod"
    module.mkdir()
    (module / "go.mod").write_text("module example.com/m\n\ngo 1.20\n")
    (module / "m_test.go").write_text(
        'package m\nimport "testing"\n'
        "func TestPass(t *testing.T) {}\n"
        'func TestFail(t *testing.T) { t.Errorf("want %d got %d", 1, 2) }\n')
    report = _finish(stack, stack.service.start(TestRunRequest(project=str(module)), stack.context()))
    assert report.runner == "go" and report.totals_source == "go_json"
    assert (report.totals.passed, report.totals.failed, report.totals.total) == (1, 1, 2)
    failure = report.failures[0]
    assert failure.id == "example.com/m.TestFail"
    assert (failure.file, failure.line) == ("m_test.go", 4)
    narrowed = _finish(stack, stack.service.start(
        TestRunRequest(project=str(module), selector="run:TestPass"), stack.context()))
    assert narrowed.status == "passed" and narrowed.totals.total == 1
