"""Playbook reports are bounded suggestions and cannot mutate owner notes."""
from datetime import date
from types import SimpleNamespace

import lesson_decay

from sonder_runtime.adapters.playbook_maintenance import MAX_AUDIT_ENTRIES, format_report, report


def entry(identifier, body, category="procedure", status="approved", when="2026-01-01"):
    return {"id": identifier, "topic": "builds", "title": "Build note", "body": body,
            "category": category, "status": status, "date": when}


class Store:
    def __init__(self, entries):
        self.entries = entries

    def list_topics(self):
        return [{"topic": "builds"}]

    def read(self, topic, approved_only=False):
        return self.entries

    def approved_index(self):
        return "- [Builds](builds.md) — procedure — open when: compiler\n"


def test_duplicate_stale_and_conflict_findings_are_report_only():
    entries = [
        entry("one", "Always enable the shared compiler cache for builds."),
        entry("two", "Never enable the shared compiler cache for builds."),
        entry("copy", "Always enable the shared compiler cache for builds."),
        entry("env", "Compiler path is C:/Tools/cl.exe.", "environment"),
        entry("bench", "Build took 12 seconds on 2026-01-01.", "measurement"),
        entry("pending", "Owner proposed a new procedure.", status="proposed"),
    ]
    before = [dict(row) for row in entries]
    result = report(Store(entries), today=date(2026, 9, 30), detect_conflicts=lesson_decay.detect_contradictions)
    assert result["report_only"] is True
    assert result["duplicate_merge_plans"][0]["supersede"] == [{"topic": "builds", "id": "copy"}]
    assert {row["id"] for row in result["stale"]} == {"env", "bench"}
    assert result["conflict_candidates"]
    assert result["conflict_candidates"][0]["evidence"] == ["lexical", "lexical"]
    assert entries == before
    assert "report-only" in "\n".join(format_report(result))


def test_category_ages_are_configurable_and_invalid_dates_flagged():
    store = Store([entry("fresh", "Compiler uses pinned version.", "environment", when="2026-09-20"),
                   entry("unknown", "Benchmark used a single worker.", "measurement", when="invalid")])
    config = SimpleNamespace(environment_stale_days=5, measurement_stale_days=100)
    result = report(store, config=config, today=date(2026, 9, 30))
    assert {row["id"] for row in result["stale"]} == {"fresh", "unknown"}


def test_maintenance_work_and_findings_are_capped():
    store = Store([entry(str(i), f"Unique entry number {i}.", status="proposed")
                   for i in range(MAX_AUDIT_ENTRIES + 1)])
    result = report(store)
    assert result["entries_checked"] == MAX_AUDIT_ENTRIES
    assert result["truncated"]
    assert result["conflict_candidates"] == []
    assert result["status_counts"] == {"proposed": MAX_AUDIT_ENTRIES}
