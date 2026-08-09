import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import fleet_store


def test_pytest_harness_never_uses_live_fleet_ledger():
    configured = os.environ.get("SONDER_FLEET_DB", "")

    assert configured
    assert Path(configured).resolve() == Path(fleet_store.database_path()).resolve()
    assert Path(configured).parent.name.startswith("sonder-pytest-fleet-")


def _row(agent_id, *, role="agent", parent_id="", task="work"):
    return {
        "id": agent_id,
        "role": role,
        "parent_id": parent_id,
        "task": task,
        "status": "queued",
        "activity": "queued",
        "started_ts": 100.0,
        "updated_ts": 100.0,
        "tokens_in": 4,
        "files": [],
    }


def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    fleet_store.reset_schema_cache_for_tests()
    fleet_store.clear_all()


def test_agent_lifecycle_is_durable_and_queryable(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-a", 101, 100.0)

    created = fleet_store.create_agent(
        _row("master-a", role="master"), "owner-a", 101,
    )
    running = fleet_store.start_agent(
        created["id"], "owner-a", "running inline", in_model_call=True,
        tool_calls=1, mode="inline", tier="code",
    )
    live_snap = fleet_store.snapshot(include_finished=False)
    finished, marker = fleet_store.finish_agent(
        created["id"], "owner-a", output="done",
    )
    snap = fleet_store.snapshot()

    assert running["status"] == "running"
    assert running["in_model_call"] is True
    assert live_snap["running_agents"] == 1
    assert live_snap["queued_agents"] == 0
    assert live_snap["active_model_calls"] == 1
    assert marker == "done"
    assert finished["status"] == "done"
    assert finished["mode"] == "inline"
    assert finished["tier"] == "code"
    assert snap["active_agents"] == 0
    assert snap["latest_master_result"] == "done"
    assert snap["latest_master"]["id"] == "master-a"
    assert snap["latest_master"]["task"] == "work"


def test_repository_project_scope_is_durable(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-project", 111, 100.0)
    row = _row("master-project", role="master")
    row["project"] = str(tmp_path / "requested-repo")

    created = fleet_store.create_agent(row, "owner-project", 111)
    fetched = fleet_store.get_agent("master-project")

    assert created["project"] == row["project"]
    assert fetched["project"] == row["project"]


def test_existing_fleet_ledger_migrates_project_scope_column(monkeypatch, tmp_path):
    database = tmp_path / "legacy-fleet.db"
    legacy_schema = fleet_store._SCHEMA.replace(
        "    project TEXT DEFAULT '',\n", "",
    )
    with sqlite3.connect(database) as conn:
        conn.executescript(legacy_schema)
    monkeypatch.setenv("SONDER_FLEET_DB", str(database))
    fleet_store.reset_schema_cache_for_tests()

    fleet_store.register_owner("owner-migration", 112, 100.0)

    with sqlite3.connect(database) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(fleet_agents)")
        }
    assert "project" in columns


def test_cancellation_prevents_queued_start_and_inherits_to_late_child(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-a", 101, 100.0)
    fleet_store.create_agent(_row("master-a", role="master"), "owner-a", 101)

    cancelled = fleet_store.cancel_agents("master-a")
    started = fleet_store.start_agent(
        "master-a", "owner-a", "should not run", in_model_call=True,
    )
    child = fleet_store.create_agent(
        _row("agent-late", parent_id="master-a"), "owner-a", 101,
    )

    assert cancelled["queued"] == 1
    assert started is None
    assert child["status"] == "cancelled"
    assert child["cancel_requested"] is True


def test_running_cancellation_discards_late_result(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-a", 101, 100.0)
    fleet_store.create_agent(_row("agent-a"), "owner-a", 101)
    fleet_store.start_agent(
        "agent-a", "owner-a", "model", in_model_call=True, tool_calls=1,
    )

    cancelled = fleet_store.cancel_agents("agent-a")
    finished, marker = fleet_store.finish_agent(
        "agent-a", "owner-a", output="late secret result",
    )

    assert cancelled["running"] == 1
    assert cancelled["model_calls"] == 1
    assert marker == "CANCELLED"
    assert finished["status"] == "cancelled"
    assert finished["output"] == ""


def test_second_process_can_cancel_first_process_worker(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-primary", 101, 100.0)
    fleet_store.create_agent(_row("agent-cross-process"), "owner-primary", 101)
    fleet_store.start_agent(
        "agent-cross-process", "owner-primary", "model",
        in_model_call=True, tool_calls=1,
    )

    script = (
        "import json, fleet_store; "
        "print(json.dumps(fleet_store.cancel_agents('agent-cross-process')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    remote = json.loads(completed.stdout)
    finished, marker = fleet_store.finish_agent(
        "agent-cross-process", "owner-primary", output="late",
    )

    assert remote["matched"] == 1
    assert remote["model_calls"] == 1
    assert marker == "CANCELLED"
    assert finished["status"] == "cancelled"


def test_stale_owner_requires_two_observations_before_interrupting(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])
    fleet_store.register_owner("owner-stale", 202, 100.0)
    fleet_store.create_agent(
        _row("master-stale", role="master"), "owner-stale", 202,
    )
    fleet_store.start_agent(
        "master-stale", "owner-stale", "running", in_model_call=True,
    )

    clock["now"] = 200.0
    first = fleet_store.reconcile_stale_owners(
        now=200.0, stale_seconds=30, grace_seconds=10,
    )
    clock["now"] = 211.0
    second = fleet_store.reconcile_stale_owners(
        now=211.0, stale_seconds=30, grace_seconds=10,
    )
    recovered = fleet_store.get_agent("master-stale")

    assert first == {"suspect_owners": 1, "interrupted": 0, "owners": []}
    assert second["interrupted"] == 1
    assert recovered["status"] == "interrupted"
    assert recovered["cancel_requested"] is True


def test_heartbeat_clears_stale_suspicion(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])
    fleet_store.register_owner("owner-live", 303, 100.0)
    fleet_store.create_agent(_row("agent-live"), "owner-live", 303)
    fleet_store.start_agent("agent-live", "owner-live", "running")

    clock["now"] = 200.0
    fleet_store.reconcile_stale_owners(
        now=200.0, stale_seconds=30, grace_seconds=10,
    )
    clock["now"] = 205.0
    assert fleet_store.heartbeat_owner("owner-live") is True
    clock["now"] = 211.0
    result = fleet_store.reconcile_stale_owners(
        now=211.0, stale_seconds=30, grace_seconds=10,
    )

    assert result["interrupted"] == 0
    assert fleet_store.get_agent("agent-live")["status"] == "running"


def test_pruning_keeps_active_rows(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-a", 101, 100.0)
    fleet_store.create_agent(_row("agent-active"), "owner-a", 101)
    fleet_store.start_agent("agent-active", "owner-a", "running")
    for index in range(15):
        agent_id = f"agent-done-{index:02d}"
        fleet_store.create_agent(_row(agent_id), "owner-a", 101)
        fleet_store.start_agent(agent_id, "owner-a", "running")
        fleet_store.finish_agent(agent_id, "owner-a", output="done")

    fleet_store.prune(finished_retention=10, event_retention=100)
    snap = fleet_store.snapshot(limit=30)

    assert snap["active_agents"] == 1
    assert fleet_store.get_agent("agent-active")["status"] == "running"
    assert snap["total_agents"] == 11


def test_successful_retry_marks_source_as_retried(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-old", 101, 100.0)
    fleet_store.create_agent(
        _row("master-old", role="master"), "owner-old", 101,
    )
    fleet_store.start_agent("master-old", "owner-old", "running")
    fleet_store.close_owner("owner-old", "simulated crash")
    fleet_store.register_owner("owner-new", 202, 200.0)
    retry = _row("master-new", role="master", task="retry task")
    retry["retry_of"] = "master-old"
    fleet_store.create_agent(retry, "owner-new", 202)
    fleet_store.start_agent("master-new", "owner-new", "retrying")

    finished, marker = fleet_store.finish_agent(
        "master-new", "owner-new", output="recovered",
    )
    source = fleet_store.get_agent("master-old")

    assert marker == "recovered"
    assert finished["status"] == "done"
    assert source["status"] == "retried"
    assert source["retried_by"] == "master-new"


def test_active_model_calls_counts_the_whole_table_not_the_page(monkeypatch):
    """A model-call count summed over the paginated agents list drops to 0 once
    active rows exceed the page size, and every GPU-contention guard reads 0 as
    'quiet'. The full-table aggregate must stay accurate.
    """
    import master_orchestrator as mo

    fleet_store.register_owner("owner-page", 900, 100.0)
    # One agent genuinely inside a model call, registered FIRST (older).
    fleet_store.create_agent(_row("in-call"), "owner-page", 900)
    # queued -> running, then into a model call (begin_model_call requires running).
    fleet_store.start_agent("in-call", "owner-page", "starting")
    fleet_store.begin_model_call("in-call", "owner-page", "generating", tool_calls=0)
    # begin_model_call stamps updated_ts=now, but in production the call started
    # earlier and a long generation is still in flight; backdate it so the
    # flooding rows genuinely sort ahead, as they do live.
    conn = fleet_store._connect()
    conn.execute("UPDATE fleet_agents SET updated_ts=1.0 WHERE id='in-call'")
    conn.commit()
    conn.close()

    # Then flood with more than the snapshot page of fresher active rows, none
    # in a model call, so they crowd the in-call row out of the page.
    page = mo.ABSOLUTE_MAX_AGENTS + 1
    for i in range(page + 5):
        fleet_store.create_agent(_row("fresh-%03d" % i), "owner-page", 900)

    snap = fleet_store.snapshot(include_finished=False, limit=page)
    # The paginated list would miss the older in-call row...
    page_sum = sum(
        1 for r in snap["agents"]
        if r.get("status") in ("queued", "running") and r.get("in_model_call")
    )
    assert page_sum == 0, "precondition: the in-call row is off the page"
    # ...but the full-table aggregate and the guard must still see it.
    assert snap["active_model_calls"] == 1
    assert mo.active_model_call_count() == 1
