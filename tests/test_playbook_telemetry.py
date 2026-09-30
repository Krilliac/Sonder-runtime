"""Loaded topics are correlations joined to canonical, provenance-aware outcomes."""
import sqlite3

import pytest

from sonder_runtime.adapters import playbook_telemetry as telemetry


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE outcomes (interaction_id TEXT, signal TEXT, source TEXT)")
    connection.execute("INSERT INTO interactions VALUES('i1')")
    yield connection
    connection.close()


def test_empty_and_unknown_usage_do_not_create_tables(conn):
    assert telemetry.log_usage(conn, [], "i1") == 0
    assert telemetry.log_usage(conn, ["builds"], "unknown") == 0
    assert telemetry.usage_report(conn)["loaded_turns"] == 0
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name='playbook_usage'").fetchone()


def test_usage_is_idempotent_and_outcomes_preserve_source(conn):
    topics = [{"topic": "builds", "entry_ids": ["one", "two"]}]
    assert telemetry.log_usage(conn, topics, "i1") == 2
    assert telemetry.log_usage(conn, topics, "i1") == 0
    conn.executemany("INSERT INTO outcomes VALUES('i1',?,?)", [
        ("tests_passed", "machine"), ("rejected", "caller"),
    ])
    result = telemetry.usage_report(conn)
    assert result["topic_loads"] == 1
    assert result["loaded_turns"] == 1
    assert {row["source"] for row in result["topics"][0]["outcomes"]} == {"machine", "caller"}
    assert {row["good"] for row in result["topics"][0]["outcomes"]} == {True, False}
    # No separate outcome writer: canonical evidence added later appears live.
    conn.execute("INSERT INTO outcomes VALUES('i1','used','attributed')")
    assert len(telemetry.usage_report(conn)["topics"][0]["outcomes"]) == 3


def test_content_is_not_persisted_and_load_budget_is_enforced(conn):
    topics = [{"topic": "builds", "entry_ids": [f"entry-{i}" for i in range(100)]},
              "../escape", "not a slug"]
    assert telemetry.log_usage(conn, topics, "i1") == 64
    columns = [row[1] for row in conn.execute("PRAGMA table_info(playbook_usage)")]
    assert columns == ["interaction_id", "topic", "entry_id", "loaded_ts"]


def test_usage_retention_is_bounded(conn, monkeypatch):
    monkeypatch.setattr(telemetry, "MAX_USAGE_ROWS", 3)
    topics = [{"topic": "builds", "entry_ids": [f"entry-{i}" for i in range(6)]}]
    telemetry.log_usage(conn, topics, "i1")
    assert conn.execute("SELECT COUNT(*) FROM playbook_usage").fetchone()[0] == 3
