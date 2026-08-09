import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

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


def test_pre_provenance_ledger_migrates_all_agent_and_event_columns(
    monkeypatch, tmp_path,
):
    database = tmp_path / "pre-provenance-fleet.db"
    agent_tail = """    retried_by TEXT DEFAULT '',
    master_task_digest TEXT DEFAULT '',
    delegated_task_digest TEXT DEFAULT '',
    objective_ids_json TEXT DEFAULT '[]',
    task_drift INTEGER DEFAULT 0,
    drift_metrics_json TEXT DEFAULT '{}'
"""
    event_columns = """    master_task_digest TEXT DEFAULT '',
    delegated_task_digest TEXT DEFAULT '',
    objective_ids_json TEXT DEFAULT '[]',
    task_drift INTEGER DEFAULT 0,
"""
    legacy_schema = fleet_store._SCHEMA.replace(
        agent_tail, "    retried_by TEXT DEFAULT ''\n",
    ).replace(event_columns, "")
    with sqlite3.connect(database) as conn:
        conn.executescript(legacy_schema)
    monkeypatch.setenv("SONDER_FLEET_DB", str(database))
    fleet_store.reset_schema_cache_for_tests()

    fleet_store.register_owner("owner-provenance-migration", 113, 100.0)

    with sqlite3.connect(database) as conn:
        agents = {
            row[1] for row in conn.execute("PRAGMA table_info(fleet_agents)")
        }
        events = {
            row[1] for row in conn.execute("PRAGMA table_info(fleet_events)")
        }
    assert {
        "master_task_digest", "delegated_task_digest", "objective_ids_json",
        "task_drift", "drift_metrics_json",
    } <= agents
    assert {
        "master_task_digest", "delegated_task_digest", "objective_ids_json",
        "task_drift",
    } <= events


def test_oversized_drift_metrics_remain_valid_auditable_json(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-drift-json", 114, 100.0)
    fleet_store.create_agent(_row("agent-drift-json"), "owner-drift-json", 114)
    fleet_store.start_agent("agent-drift-json", "owner-drift-json", "run")

    stored, _marker = fleet_store.finish_agent(
        "agent-drift-json", "owner-drift-json",
        error="drift", task_drift=True,
        drift_metrics={"evidence": "x" * 10_000},
    )

    assert stored["status"] == "task_drift"
    assert stored["drift_metrics"]["audit_truncated"] is True
    assert len(stored["drift_metrics"]["sha256"]) == 64


def test_provenance_fields_cannot_be_rewritten_by_generic_updates(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-immutable", 115, 100.0)
    row = _row("agent-immutable")
    row.update({
        "master_task_digest": "a" * 64,
        "delegated_task_digest": "b" * 64,
        "objective_ids": ["one"],
    })
    fleet_store.create_agent(row, "owner-immutable", 115)

    updated = fleet_store.update_agent(
        "agent-immutable", "owner-immutable",
        master_task_digest="c" * 64,
        delegated_task_digest="d" * 64,
        objective_ids=[],
        task_drift=True,
        drift_metrics={"forged": True},
        activity="still queued",
    )

    assert updated["master_task_digest"] == "a" * 64
    assert updated["delegated_task_digest"] == "b" * 64
    assert updated["objective_ids"] == ["one"]
    assert updated["task_drift"] is False
    assert updated["drift_metrics"] == {}


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


def _message_tree(owner_id="owner-messages", project="project-a"):
    fleet_store.register_owner(owner_id, 707, 100.0)
    master = _row("master-messages", role="master")
    master["project"] = project
    fleet_store.create_agent(master, owner_id, 707)
    child = _row("agent-messages", parent_id=master["id"])
    child["project"] = project
    fleet_store.create_agent(child, owner_id, 707)
    return master["id"], child["id"]


def test_retained_message_has_queued_and_atomic_delivered_receipts(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    fleet_store.start_agent(child_id, "owner-messages", "working")

    queued = fleet_store.queue_agent_message(
        master_id, child_id, "owner-messages",
        project="project-a", mode="steer", body="check the boundary first",
        now=200.0,
    )
    delivered = fleet_store.claim_agent_messages(
        child_id, "owner-messages", project="project-a", now=201.0,
    )
    claimed_again = fleet_store.claim_agent_messages(
        child_id, "owner-messages", project="project-a", now=202.0,
    )

    assert queued["status"] == "queued"
    assert queued["delivered_ts"] is None
    assert delivered == [{**queued, "status": "delivered", "delivered_ts": 201.0}]
    assert claimed_again == []
    assert fleet_store.list_agent_messages(
        master_id, "owner-messages", project="project-a",
    )[0]["message_id"] == queued["message_id"]


def test_message_scope_rejects_other_owner_project_and_parent_tree(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    other = _row("master-other", role="master")
    other["project"] = "project-a"
    fleet_store.create_agent(other, "owner-messages", 707)

    for kwargs in (
        {"owner_id": "wrong-owner", "project": "project-a"},
        {"owner_id": "owner-messages", "project": "wrong-project"},
    ):
        with pytest.raises(PermissionError):
            fleet_store.queue_agent_message(
                master_id, child_id, kwargs["owner_id"],
                project=kwargs["project"], mode="follow_up", body="next",
            )
    with pytest.raises(PermissionError, match="same parent scope"):
        fleet_store.queue_agent_message(
            master_id, other["id"], "owner-messages",
            project="project-a", mode="follow_up", body="next",
        )


def test_steer_requires_active_recipient_and_expires_when_agent_finishes(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    with pytest.raises(ValueError, match="queued or running"):
        fleet_store.finish_agent(child_id, "owner-messages", output="done")
        fleet_store.queue_agent_message(
            master_id, child_id, "owner-messages",
            project="project-a", mode="steer", body="too late",
        )

    # A queued follow-up survives completion because it is explicitly intended
    # for a later retained turn; queued steering does not.
    child = _row("agent-active", parent_id=master_id)
    child["project"] = "project-a"
    fleet_store.create_agent(child, "owner-messages", 707)
    steer = fleet_store.queue_agent_message(
        master_id, child["id"], "owner-messages",
        project="project-a", mode="steer", body="adjust now", now=300.0,
    )
    follow_up = fleet_store.queue_agent_message(
        master_id, child["id"], "owner-messages",
        project="project-a", mode="follow_up", body="continue later", now=301.0,
    )
    fleet_store.start_agent(child["id"], "owner-messages", "working")
    fleet_store.finish_agent(child["id"], "owner-messages", output="done")
    statuses = {
        row["message_id"]: row["status"] for row in fleet_store.list_agent_messages(
            child["id"], "owner-messages", project="project-a",
        )
    }
    assert statuses[steer["message_id"]] == "expired"
    assert statuses[follow_up["message_id"]] == "queued"


def test_message_rate_size_discovery_and_pruning_are_bounded(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    discovered = fleet_store.list_agents_scoped(
        "owner-messages", project="project-a", parent_id=master_id,
    )
    assert {row["id"] for row in discovered} == {master_id, child_id}
    assert fleet_store.list_agents_scoped(
        "owner-messages", project="missing",
    ) == []
    with pytest.raises(ValueError, match="exceeds"):
        fleet_store.queue_agent_message(
            master_id, child_id, "owner-messages", project="project-a",
            mode="follow_up", body="x" * (fleet_store.MAX_MESSAGE_CHARS + 1),
        )
    receipts = []
    for index in range(fleet_store.MAX_MESSAGES_PER_RATE_WINDOW):
        receipts.append(fleet_store.queue_agent_message(
            master_id, child_id, "owner-messages", project="project-a",
            mode="follow_up", body="message %s" % index, now=400.0 + index,
        ))
    with pytest.raises(RuntimeError, match="rate limit"):
        fleet_store.queue_agent_message(
            master_id, child_id, "owner-messages", project="project-a",
            mode="follow_up", body="one too many", now=409.5,
        )
    fleet_store.claim_agent_messages(
        child_id, "owner-messages", project="project-a", limit=32, now=500.0,
    )
    monkeypatch.setattr(fleet_store.time, "time", lambda: 10_000.0)
    result = fleet_store.prune(
        message_retention_seconds=3600,
    )
    assert result["messages"] == len(receipts)
    assert fleet_store.list_agent_messages(
        child_id, "owner-messages", project="project-a",
    ) == []


def test_stable_principal_rediscovers_and_messages_tree_after_owner_restart(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    principal = "principal-" + "a" * 32
    secret = "restart-secret-" + "b" * 48
    fleet_store.register_principal(principal, secret)
    fleet_store.register_owner("owner-before", 1001, 100.0)
    master = _row("master-retained", role="master")
    master["project"] = "project-retained"
    fleet_store.create_agent(
        master, "owner-before", 1001, principal_id=principal,
        principal_secret=secret,
    )
    child = _row("agent-retained", parent_id=master["id"])
    child["project"] = master["project"]
    fleet_store.create_agent(
        child, "owner-before", 1001, principal_id=principal,
        principal_secret=secret,
    )
    fleet_store.close_owner("owner-before", "simulated restart")
    fleet_store.register_owner("owner-after", 1002, 200.0)

    rediscovered = fleet_store.list_agents_scoped(
        "owner-after", project=master["project"], parent_id=master["id"],
        principal_id=principal, principal_secret=secret,
    )
    queued = fleet_store.queue_agent_message(
        master["id"], child["id"], "owner-after", project=master["project"],
        mode="follow_up", body="continue after restart",
        principal_id=principal, principal_secret=secret,
    )
    delivered = fleet_store.claim_agent_messages(
        child["id"], "owner-after", project=master["project"],
        principal_id=principal, principal_secret=secret,
    )

    assert {row["id"] for row in rediscovered} == {master["id"], child["id"]}
    assert queued["owner_id"] == "owner-after"
    assert delivered[0]["message_id"] == queued["message_id"]
    with pytest.raises(PermissionError, match="authentication"):
        fleet_store.list_agents_scoped(
            "owner-after", project=master["project"],
            principal_id=principal, principal_secret="wrong-secret-" + "x" * 32,
        )


def test_scoped_discovery_from_child_returns_only_that_subtree(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    sibling = _row("agent-sibling", parent_id=master_id)
    sibling["project"] = "project-a"
    fleet_store.create_agent(sibling, "owner-messages", 707)
    grandchild = _row("agent-grandchild", parent_id=child_id)
    grandchild["project"] = "project-a"
    fleet_store.create_agent(grandchild, "owner-messages", 707)

    subtree = fleet_store.list_agents_scoped(
        "owner-messages", project="project-a", parent_id=child_id,
    )

    assert {row["id"] for row in subtree} == {child_id, grandchild["id"]}


def test_prune_preserves_unexpired_message_endpoints_and_ancestor_chain(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    fleet_store.register_owner("owner-prune-message", 808, 100.0)
    project = "project-prune"
    root = _row("root-prune", role="master")
    root["project"] = project
    fleet_store.create_agent(root, "owner-prune-message", 808)
    parent = _row("parent-prune", parent_id=root["id"])
    parent["project"] = project
    fleet_store.create_agent(parent, "owner-prune-message", 808)
    child = _row("child-prune", parent_id=parent["id"])
    child["project"] = project
    fleet_store.create_agent(child, "owner-prune-message", 808)
    for agent_id in (root["id"], parent["id"], child["id"]):
        fleet_store.start_agent(agent_id, "owner-prune-message", "running")
        fleet_store.finish_agent(agent_id, "owner-prune-message", output="done")
    receipt = fleet_store.queue_agent_message(
        parent["id"], child["id"], "owner-prune-message", project=project,
        mode="follow_up", body="retained past agent row retention", now=1_000.0,
        pending_ttl_seconds=7 * 24 * 60 * 60,
    )
    # Force the referenced tree beyond the minimum finished-row retention.
    for index in range(12):
        row = _row("newer-finished-%02d" % index)
        row["project"] = project
        fleet_store.create_agent(row, "owner-prune-message", 808)
        fleet_store.start_agent(row["id"], "owner-prune-message", "running")
        fleet_store.finish_agent(row["id"], "owner-prune-message", output="done")
    with fleet_store._write_transaction() as conn:
        conn.execute(
            "UPDATE fleet_agents SET updated_ts=1 WHERE id IN (?, ?, ?)",
            (root["id"], parent["id"], child["id"]),
        )
    monkeypatch.setattr(fleet_store.time, "time", lambda: 2_000.0)

    fleet_store.prune(finished_retention=10)

    assert fleet_store.get_agent(root["id"])["id"] == root["id"]
    assert fleet_store.get_agent(parent["id"])["parent_id"] == root["id"]
    assert fleet_store.get_agent(child["id"])["parent_id"] == parent["id"]
    assert fleet_store.list_agent_messages(
        child["id"], "owner-prune-message", project=project,
    )[0]["message_id"] == receipt["message_id"]
    with fleet_store._connect() as conn:
        assert fleet_store._scope_root_id(conn, child["id"]) == root["id"]


def test_legacy_parent_rejects_partial_principal_adoption(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    principal = "principal-" + "a" * 32
    secret = "principal-secret-" + "b" * 48
    fleet_store.register_principal(principal, secret)
    fleet_store.register_owner("owner-legacy", 909, 100.0)
    parent = _row("legacy-parent", role="master")
    parent["project"] = "project-a"
    fleet_store.create_agent(parent, "owner-legacy", 909)
    adopted = _row("adopted-child", parent_id=parent["id"])
    adopted["project"] = parent["project"]

    with pytest.raises(PermissionError, match="principal"):
        fleet_store.create_agent(
            adopted, "owner-legacy", 909, principal_id=principal,
            principal_secret=secret,
        )


def test_prune_honors_each_queued_messages_persisted_expiration(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    master_id, child_id = _message_tree()
    receipt = fleet_store.queue_agent_message(
        master_id, child_id, "owner-messages", project="project-a",
        mode="follow_up", body="later", now=1_000.0,
        pending_ttl_seconds=7 * 24 * 60 * 60,
    )
    monkeypatch.setattr(
        fleet_store.time, "time", lambda: 1_000.0 + 2 * 24 * 60 * 60,
    )

    fleet_store.prune()

    messages = fleet_store.list_agent_messages(
        child_id, "owner-messages", project="project-a",
    )
    assert messages[0]["message_id"] == receipt["message_id"]
    assert messages[0]["status"] == "queued"


def test_principal_parent_rejects_child_from_different_principal(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    principal_a = "principal-" + "a" * 32
    principal_b = "principal-" + "b" * 32
    secret_a = "secret-a-" + "a" * 48
    secret_b = "secret-b-" + "b" * 48
    fleet_store.register_principal(principal_a, secret_a)
    fleet_store.register_principal(principal_b, secret_b)
    fleet_store.register_owner("owner-principals", 910, 100.0)
    parent = _row("principal-parent", role="master")
    parent["project"] = "project-a"
    fleet_store.create_agent(
        parent, "owner-principals", 910, principal_id=principal_a,
        principal_secret=secret_a,
    )
    child = _row("foreign-principal-child", parent_id=parent["id"])
    child["project"] = parent["project"]

    with pytest.raises(PermissionError, match="principal"):
        fleet_store.create_agent(
            child, "owner-principals", 910, principal_id=principal_b,
            principal_secret=secret_b,
        )


def test_first_run_principal_publication_is_atomic_and_retry_safe(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    credential = tmp_path / "fleet-principal.json"
    monkeypatch.setenv("SONDER_FLEET_PRINCIPAL_FILE", str(credential))
    original_link = fleet_store.os.link
    publication_barrier = threading.Barrier(2)

    def racing_link(source, destination):
        publication_barrier.wait(timeout=5)
        return original_link(source, destination)

    monkeypatch.setattr(fleet_store.os, "link", racing_link)
    results = []
    failures = []

    def load_credentials():
        try:
            results.append(fleet_store.local_principal_credentials())
        except Exception as exc:  # surfaced below with full object for diagnosis
            failures.append(exc)

    threads = [threading.Thread(target=load_credentials) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert json.loads(credential.read_text(encoding="utf-8")) == {
        "principal_id": results[0][0], "secret": results[0][1],
    }
    assert list(tmp_path.glob(".fleet-principal.json.*.tmp")) == []


def test_existing_principal_file_with_wrong_secret_fails_closed_without_clobber(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    credential = tmp_path / "fleet-principal.json"
    monkeypatch.setenv("SONDER_FLEET_PRINCIPAL_FILE", str(credential))
    principal = "principal-" + "c" * 32
    correct_secret = "correct-secret-" + "d" * 48
    fleet_store.register_principal(principal, correct_secret)
    payload = {
        "principal_id": principal,
        "secret": "wrong-secret-" + "e" * 48,
    }
    original = json.dumps(payload, sort_keys=True) + "\n"
    credential.write_text(original, encoding="utf-8")

    with pytest.raises(PermissionError, match="authentication"):
        fleet_store.local_principal_credentials()

    assert credential.read_text(encoding="utf-8") == original
