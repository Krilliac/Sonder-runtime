"""Focused tests for packaged bootstrap doctor-check policy."""

from types import SimpleNamespace

from sonder_runtime.bootstrap.doctor_checks import (
    summarize_memory_quality,
    summarize_self_heal,
)


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


def test_summarize_memory_quality_closes_connection_and_reports_severity():
    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    seen = []

    def connect(path):
        seen.append(path)
        return connection

    result = summarize_memory_quality(
        connect,
        lambda conn: {
            "total_lessons": 8,
            "missing_fts": 1,
            "no_embedding": 4,
        },
        "memory.sqlite",
    )

    assert result == {"status": "fail", "detail": "8 lessons, 1 severe issue(s)"}
    assert seen == ["memory.sqlite"]
    assert connection.closed


def test_summarize_memory_quality_preserves_connection_and_audit_failures():
    closed = []

    class Connection:
        def close(self):
            closed.append(True)

    assert summarize_memory_quality(
        lambda _path: (_ for _ in ()).throw(OSError("locked")),
        lambda _conn: {},
        "memory.sqlite",
    ) == {"status": "skipped", "detail": "cannot open memory db (locked)"}
    assert summarize_memory_quality(
        lambda _path: Connection(),
        lambda _conn: (_ for _ in ()).throw(RuntimeError("offline")),
        "memory.sqlite",
    ) == {"status": "skipped", "detail": "audit failed (offline)"}
    assert closed == [True]
