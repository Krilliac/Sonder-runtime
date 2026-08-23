"""Focused tests for packaged bootstrap doctor-check policy."""

from types import SimpleNamespace

from sonder_runtime.bootstrap.doctor_checks import (
    bounded_join,
    summarize_memory_quality,
    summarize_self_heal,
    summarize_worker_probe,
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


def test_bounded_join_passes_through_short_lists():
    assert bounded_join(["a", "b"]) == "a, b"
    assert bounded_join([]) == ""


def test_bounded_join_caps_long_lists_with_a_remainder_marker():
    items = ["w%d" % i for i in range(12)]
    assert bounded_join(items, limit=8) == (
        "w0, w1, w2, w3, w4, w5, w6, w7, +4 more"
    )


def test_summarize_worker_probe_ok_when_every_worker_answers():
    assert summarize_worker_probe(["a: 1 models", "b: 2 models"], [], 2) == {
        "status": "ok",
        "detail": "2 worker(s) reachable: a: 1 models, b: 2 models",
    }


def test_summarize_worker_probe_warns_on_partial_outage():
    result = summarize_worker_probe(["a: 1 models"], ["b (connection refused)"], 2)
    assert result["status"] == "warn"
    assert "1/2 worker(s) unreachable" in result["detail"]
    assert "b (connection refused)" in result["detail"]
    assert "reachable: a: 1 models" in result["detail"]


def test_summarize_worker_probe_fails_when_every_worker_is_unreachable():
    result = summarize_worker_probe(
        [], ["a (timed out)", "b (timed out)"], 2
    )
    assert result == {
        "status": "fail",
        "detail": "2/2 worker(s) unreachable: a (timed out), b (timed out)",
    }
