"""Focused tests for packaged bootstrap doctor-check policy."""

from types import SimpleNamespace

from sonder_runtime.bootstrap.doctor_checks import summarize_self_heal


def test_summarize_self_heal_reports_clean_database():
    seen = []

    def check(path):
        seen.append(path)
        return []

    assert summarize_self_heal(check, "state.sqlite") == {
        "status": "ok",
        "detail": "no issues",
    }
    assert seen == ["state.sqlite"]


def test_summarize_self_heal_warns_when_all_findings_are_repairable():
    issues = [SimpleNamespace(repairable=True), SimpleNamespace(repairable=True)]

    assert summarize_self_heal(lambda _path: issues, "state.sqlite") == {
        "status": "warn",
        "detail": "2 issue(s), 2 repairable",
    }


def test_summarize_self_heal_fails_when_any_finding_is_not_repairable():
    issues = [SimpleNamespace(repairable=True), SimpleNamespace(repairable=False)]

    assert summarize_self_heal(lambda _path: issues, "state.sqlite") == {
        "status": "fail",
        "detail": "2 issue(s), 1 repairable",
    }


def test_summarize_self_heal_converts_inspection_failure_to_skipped():
    def check(_path):
        raise RuntimeError("offline")

    assert summarize_self_heal(check, "state.sqlite") == {
        "status": "skipped",
        "detail": "self-heal check failed (offline)",
    }
