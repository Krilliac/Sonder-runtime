"""Focused contract tests for the injected context-health application slice."""
from __future__ import annotations

from sonder_runtime.application.context_health import (
    ContextHealthService,
    ContextHealthSettings,
    ContextMemorySnapshot,
)


class Identity:
    def session(self, value):
        return "default" if not value else value

    def project(self, value):
        return "main" if not value else value


class Repository:
    def snapshot(self, session, project, limit):
        assert limit == 2
        return ContextMemorySnapshot(
            turns=(("a", "b"), ("c", "d"), ("old", "turn")),
            total_turns=3, title="Demo", summary="summary",
            summarized_through="turn-1", updated_ts="now", sessions=4,
            lessons=2, facts=3, preferences=1, interactions=5, outcomes=4,
        )


class Policy:
    def policy(self, requested):
        assert requested == 100
        return {"requested": 100, "native": 80, "native_max": 80,
                "virtual_max": 1000, "virtual": True, "mode": "virtual"}


class Metrics:
    def tokens(self, value):
        return len(value)

    def tokens_from_chars(self, value):
        return value // 2

    def bar(self, ratio):
        return "bar:%.2f" % ratio


def service():
    return ContextHealthService(
        identity=Identity(), repository=Repository(), policy=Policy(),
        metrics=Metrics(), settings=ContextHealthSettings(100, 2, "db", "home"),
    )


def test_snapshot_is_computed_from_injected_ports_and_is_bounded():
    result = service().snapshot()
    assert result["session"] == "default"
    assert result["project"] == "main"
    assert result["live_turns"] == 3
    assert result["total_turns"] == 3
    assert result["estimated_tokens"] == 12
    assert result["context_limit"] == 100
    assert result["memory_percent"] == 1.0
    assert result["db_path"] == "db"


def test_snapshot_preserves_explicit_selectors():
    result = service().snapshot("s-1", "p-1")
    assert (result["session"], result["project"]) == ("s-1", "p-1")
