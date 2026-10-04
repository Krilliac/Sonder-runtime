"""Real SQLite event projections without filesystem or live runtime access."""
import sqlite3

import pytest

from sonder_runtime.adapters.persistence import autopilot_store, fleet_store, http_work_runs


class _Connection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        pass


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_existing_http_run_migrates_without_changing_data(database, monkeypatch):
    database.executescript(http_work_runs._SCHEMA.replace(
        ",\n    narration TEXT NOT NULL DEFAULT '{}'", ""))
    database.execute("INSERT INTO http_work_runs(run_id, owner_scope, process_id, status, created_ts, updated_ts, deadline_ts, output) VALUES ('wr-old','alice','p','returned',1,2,3,'old answer')")
    database.commit()
    http_work_runs._ensure_narration_column(database)
    http_work_runs._ensure_narration_column(database)
    monkeypatch.setattr(http_work_runs, "_connect", lambda: _Connection(database))
    original = http_work_runs.get("wr-old", owner_scope="alice")
    assert original["status"] == "returned" and original["output"] == "old answer"
    assert original["narration"] == {} and original["acknowledgement"] == ""
    http_work_runs.set_narration("wr-old", acknowledgement="Plan.", result_receipt={
        "source_event_id": "source-1", "return_event_id": "return-1", "unsupported": "omit"})
    http_work_runs.link_run("wr-old", "fleet", "fleet-1")
    updated = http_work_runs.get("wr-old", owner_scope="alice")
    assert updated["output"] == "old answer"
    assert updated["result_receipt"] == {"source_event_id": "source-1", "return_event_id": "return-1"}
    assert updated["narration"]["links"] == [{"kind": "fleet", "id": "fleet-1"}]
    assert http_work_runs.get("wr-old", owner_scope="bob") is None


def test_fleet_event_query_is_exact_bounded_and_chronological(database, monkeypatch):
    database.execute("CREATE TABLE fleet_events(event_id INTEGER PRIMARY KEY, ts REAL, stamp TEXT, agent_id TEXT, message TEXT, master_task_digest TEXT, delegated_task_digest TEXT, objective_ids_json TEXT, task_drift INTEGER)")
    for number, identity in enumerate(["ours", "other", "ours", "ours"], 1):
        database.execute("INSERT INTO fleet_events VALUES (?, ?, '12:00:00', ?, ?, '', '', '[]', 0)",
                         (number, number, identity, "event " + str(number)))
    monkeypatch.setattr(fleet_store, "_connect", lambda: _Connection(database))
    rows = fleet_store.events_for_agents({"ours"}, limit=2)
    assert [row["event_id"] for row in rows] == [3, 4]
    assert [row["recorded_ts"] for row in rows] == [3, 4]
    assert fleet_store.events_for_agents([]) == []


def test_autopilot_snapshot_keeps_owner_bound_events(database, monkeypatch):
    database.executescript(autopilot_store._SCHEMA)
    monkeypatch.setattr(autopilot_store, "_connect", lambda: _Connection(database))
    ours = autopilot_store.create_run("inspect repo", request_owner="alice")
    other = autopilot_store.create_run("private unrelated objective", request_owner="bob")
    data = autopilot_store.snapshot(request_owner="alice")
    assert [run["id"] for run in data["runs"]] == [ours["id"]]
    assert other["id"] not in str(data)
    assert data["progress"]
    assert {line["run_id"] for line in data["progress"]} == {ours["id"]}
