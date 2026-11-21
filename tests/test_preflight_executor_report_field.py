"""PreflightExecutor must read PreflightReport's real ``ok`` field.

The executor once read ``getattr(report, "passed", True)``; PreflightReport has
no ``passed`` attribute, so the degraded-state warning could never fire.
"""
import logging

import sonder_runtime.adapters.preflight as implementation
from sonder_runtime.adapters.preflight_executor import PreflightExecutor
from sonder_runtime.application.ports.preflight import CheckResult, PreflightReport

LOGGER = "sonder_runtime.adapters.preflight_executor"
WARNING_TEXT = "preflight check did not pass"


def _report(ok):
    return PreflightReport(checks=(
        CheckResult(name="state", ok=ok, required=True, detail="fake"),
    ))


def _run(monkeypatch, report):
    monkeypatch.setattr(implementation, "run_preflight", lambda *a, **k: report)
    return PreflightExecutor().run(object(), check_ollama=False)


def test_failed_report_logs_one_degraded_warning(monkeypatch, caplog):
    report = _report(ok=False)
    assert report.ok is False
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert _run(monkeypatch, report) is report
    failures = [r for r in caplog.records
                if r.name == LOGGER and WARNING_TEXT in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.WARNING]


def test_passing_report_logs_no_degraded_warning(monkeypatch, caplog):
    report = _report(ok=True)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert _run(monkeypatch, report) is report
    assert not [r for r in caplog.records
                if r.name == LOGGER and WARNING_TEXT in r.getMessage()]
