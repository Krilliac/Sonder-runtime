"""Regression tests for stale/orphaned fleet lane reconciliation and stall-warning dedupe.

Covers the production warning storm where idle synthetic interactive-lane roots
(``lane-root-...``) or persistently stalled fleet agents emitted repeated
``WARNING`` lines on every ``/v1/sonder/status`` poll.
"""
from __future__ import annotations

import logging
import os

import sonder_runtime.adapters.persistence.fleet_store as fleet_store
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.adapters.process_liveness import (
    PROCESS_ALIVE,
    PROCESS_DEAD,
)


def _isolated_store(monkeypatch, tmp_path, name: str = "fleet.db"):
    db_path = tmp_path / name
    monkeypatch.setenv("SONDER_FLEET_DB", str(db_path))
    monkeypatch.delenv("SONDER_FLEET_STALL_WARN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("SONDER_TIMEOUT", raising=False)
    fleet_store.reset_schema_cache_for_tests()
    fleet_store.clear_all()
    return db_path


def _agent_row(
    agent_id: str,
    *,
    role: str = "agent",
    parent_id: str = "",
    status: str = "queued",
    ts: float = 100.0,
) -> dict:
    return {
        "id": agent_id,
        "role": role,
        "parent_id": parent_id,
        "task": "work",
        "status": status,
        "activity": status,
        "started_ts": ts,
        "updated_ts": ts,
        "tokens_in": 4,
        "files": [],
    }


def _insert_synthetic_interactive_root(
    root_id: str,
    *,
    ts: float,
    principal_id: str = "principal-test",
    cancel_requested: int = 0,
) -> None:
    with fleet_store._write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO fleet_agents(
                id, owner_id, owner_pid, principal_id, role, parent_id, task,
                status, activity, started_ts, updated_ts, cancel_requested
            ) VALUES (?, ?, 0, ?, ?, '', 'Interactive agent lane (scoped transcript)',
                      'running', '', ?, ?, ?)
            """,
            (
                root_id,
                fleet_store.INTERACTIVE_LANE_OWNER_ID,
                principal_id,
                fleet_store.INTERACTIVE_LANE_ROLE,
                ts,
                ts,
                cancel_requested,
            ),
        )


def _insert_interactive_child(
    child_id: str,
    parent_id: str,
    *,
    status: str,
    ts: float,
    principal_id: str = "principal-test",
    in_model_call: int = 0,
) -> None:
    with fleet_store._write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO fleet_agents(
                id, owner_id, owner_pid, principal_id, role, parent_id, task,
                status, activity, started_ts, updated_ts, in_model_call
            ) VALUES (?, ?, 0, ?, ?, ?, 'Interactive agent lane (scoped transcript)',
                      ?, ?, ?, ?, ?)
            """,
            (
                child_id,
                fleet_store.INTERACTIVE_LANE_OWNER_ID,
                principal_id,
                fleet_store.INTERACTIVE_LANE_ROLE,
                parent_id,
                status,
                status,
                ts,
                ts,
                in_model_call,
            ),
        )


def _stall_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "stalled:" in record.getMessage()
    ]


def test_idle_synthetic_interactive_root_reconciled_past_deadline_with_audit_intact(
    monkeypatch, tmp_path, caplog,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "a" * 32
    child_id = "lane-child-completed"
    _insert_synthetic_interactive_root(root_id, ts=1_000.0)
    _insert_interactive_child(child_id, root_id, status="completed", ts=1_050.0)
    fleet_store.add_event(root_id, fleet_store.INTERACTIVE_LANE_OWNER_ID, "00:00:01", "spawned")
    with fleet_store._write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO fleet_messages(
                message_id, sender_id, recipient_id, owner_id, principal_id,
                project_scope, scope_root_id, mode, body, status, queued_ts, expires_ts
            ) VALUES ('msg-audit-1', ?, ?, ?, 'principal-test', '', ?, 'follow_up',
                      'retained report', 'delivered', 1050.0, 999999.0)
            """,
            (child_id, root_id, fleet_store.INTERACTIVE_LANE_OWNER_ID, root_id),
        )

    # Simulate the production 4-day idle gap (348830s > 360.0s deadline).
    clock["now"] = 1_050.0 + 348_830.0
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)

    first = fleet_store.snapshot()
    second = fleet_store.snapshot()
    third = fleet_store.snapshot()

    assert first["reconcile"].get("retired_interactive_roots") == [root_id]
    assert second["reconcile"] == {"suspect_owners": 0, "interrupted": 0, "owners": []}
    for snap in (first, second, third):
        assert snap["active_agents"] == 0
        assert snap["running_agents"] == 0
        assert snap["stalled_agent_count"] == 0
        assert snap["stalled_agent_total"] == 0
        assert snap["stalled_agents"] == []
    assert _stall_warnings(caplog) == []

    # Audit history (agent row, events, mailbox messages) stays completely intact.
    retired = fleet_store.get_agent(root_id)
    assert retired is not None
    assert retired["status"] == "done"
    assert retired["finished_ts"] == clock["now"]
    assert "reconciled idle interactive lane root" in retired["activity"]
    events = fleet_store.events_for_agents([root_id])
    assert len(events) == 1
    assert events[0]["message"] == "spawned"
    with fleet_store._connect() as conn:
        msg = conn.execute(
            "SELECT message_id, body FROM fleet_messages WHERE message_id='msg-audit-1'"
        ).fetchone()
    assert msg is not None
    assert msg["body"] == "retained report"


def test_synthetic_interactive_root_not_retired_or_stalled_while_child_is_active(
    monkeypatch, tmp_path, caplog,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "b" * 32
    child_id = "lane-child-running"
    _insert_synthetic_interactive_root(root_id, ts=1_000.0)
    # Root was created 600s ago (> 360s deadline), while its child was updated 5s ago.
    _insert_interactive_child(child_id, root_id, status="running", ts=1_595.0)
    clock["now"] = 1_600.0
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)

    snap = fleet_store.snapshot()

    assert fleet_store.get_agent(root_id)["status"] == "running"
    assert snap["stalled_agent_count"] == 0
    assert snap["stalled_agent_total"] == 0
    assert snap["stalled_agents"] == []
    root_row = next(row for row in snap["agents"] if row["id"] == root_id)
    assert root_row["stalled"] is False
    assert _stall_warnings(caplog) == []


def test_synthetic_interactive_root_respects_recent_child_activity_before_retiring(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "c" * 32
    _insert_synthetic_interactive_root(root_id, ts=1_000.0)
    _insert_interactive_child("lane-child-recent", root_id, status="completed", ts=1_480.0)

    # Root itself is 500s old, but its child finished only 20s ago (< 360s deadline).
    clock["now"] = 1_500.0
    before = fleet_store.snapshot()
    assert fleet_store.get_agent(root_id)["status"] == "running"
    assert before["stalled_agent_count"] == 0

    # Once 361s have elapsed since the child finished, the idle root retires cleanly.
    clock["now"] = 1_480.0 + 361.0
    after = fleet_store.snapshot()
    assert after["reconcile"].get("retired_interactive_roots") == [root_id]
    assert fleet_store.get_agent(root_id)["status"] == "done"


def test_synthetic_interactive_root_waits_for_parent_grant_expiry_without_stalling(
    monkeypatch, tmp_path, caplog,
):
    db_path = _isolated_store(monkeypatch, tmp_path)
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(db_path, sessions)
    grant = store.open_parent("principal-grant")
    with store.connect() as conn:
        root_id = conn.execute(
            "SELECT agent_id FROM agent_lane_roots WHERE session_id=?",
            (grant["parent_session_id"],),
        ).fetchone()[0]

    # Advance past the 360s progress deadline, but BEFORE the 3600s grant expires.
    clock = {"now": grant["expires_at"] - 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)

    mid_snap = fleet_store.snapshot()
    assert fleet_store.get_agent(root_id)["status"] == "running"
    assert mid_snap["stalled_agent_count"] == 0
    assert _stall_warnings(caplog) == []

    # After the parent grant expires (and idle past deadline), the root retires.
    clock["now"] = grant["expires_at"] + 10.0
    expired_snap = fleet_store.snapshot()
    assert expired_snap["reconcile"].get("retired_interactive_roots") == [root_id]
    assert fleet_store.get_agent(root_id)["status"] == "done"


def test_retired_interactive_root_still_cascades_cancel_to_new_active_child(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "d" * 32
    _insert_synthetic_interactive_root(root_id, ts=1_000.0)
    clock["now"] = 2_000.0
    fleet_store.snapshot()
    assert fleet_store.get_agent(root_id)["status"] == "done"

    # A new child lane later attaches to the same parent session root.
    _insert_interactive_child("lane-child-later", root_id, status="queued", ts=2_010.0)
    cancelled = fleet_store.cancel_agents(root_id)

    assert cancelled["matched"] == 1
    assert cancelled["agent_ids"] == ["lane-child-later"]
    assert fleet_store.get_agent("lane-child-later")["status"] == "cancelled"
    assert fleet_store.get_agent(root_id)["status"] == "done"


def test_cancelled_interactive_root_reconciled_during_snapshot(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "e" * 32
    _insert_synthetic_interactive_root(root_id, ts=1_000.0, cancel_requested=1)
    _insert_interactive_child("lane-child-done", root_id, status="completed", ts=1_005.0)

    clock["now"] = 1_010.0
    snap = fleet_store.snapshot()

    assert snap["reconcile"].get("retired_interactive_roots") == [root_id]
    assert fleet_store.get_agent(root_id)["status"] == "cancelled"
    assert snap["active_agents"] == 0


def test_stale_child_agent_lane_projection_synced_and_root_retired(monkeypatch, tmp_path):
    db_path = _isolated_store(monkeypatch, tmp_path)
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(db_path, sessions)

    with store.transaction() as tx:
        root_id = tx.root("session-sync", "principal-sync")
        lane = {
            "id": "lane-sync-1",
            "principal_id": "principal-sync",
            "parent_session_id": "session-sync",
            "root_id": root_id,
            "mailbox_parent": root_id,
            "task": "check sync",
            "status": "completed",
            "revision": 1,
        }
        tx.insert(lane)
        # Simulate a stale fleet_agents projection left running while agent_lanes is terminal.
        tx.conn.execute(
            "UPDATE fleet_agents SET status='running', updated_ts=100.0 WHERE id IN (?, ?)",
            (root_id, "lane-sync-1"),
        )

    clock = {"now": 1_000.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    snap = fleet_store.snapshot()

    assert fleet_store.get_agent("lane-sync-1")["status"] == "completed"
    assert fleet_store.get_agent(root_id)["status"] == "done"
    assert snap["active_agents"] == 0
    assert snap["stalled_agent_count"] == 0


def test_closed_owner_orphaned_agent_reconciled_cleanly(monkeypatch, tmp_path, caplog):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    fleet_store.register_owner("owner-closed", os.getpid(), 100.0)
    fleet_store.close_owner("owner-closed", "shutdown")
    # Simulate a late active insert under the already-closed owner.
    fleet_store.create_agent(
        _agent_row("agent-after-close", status="running", ts=100.0),
        "owner-closed",
        os.getpid(),
    )
    fleet_store.add_event("agent-after-close", "owner-closed", "00:00:01", "started")

    clock["now"] = 1_000.0
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)
    snap = fleet_store.snapshot()

    assert snap["reconcile"]["interrupted"] == 1
    assert snap["reconcile"]["reconciled_orphans"] == ["agent-after-close"]
    assert snap["active_agents"] == 0
    assert snap["stalled_agent_count"] == 0
    assert _stall_warnings(caplog) == []
    row = fleet_store.get_agent("agent-after-close")
    assert row["status"] == "interrupted"
    assert row["cancel_requested"] is True
    assert row["finished_ts"] == 1_000.0
    assert len(fleet_store.events_for_agents(["agent-after-close"])) == 1


def test_missing_owner_orphan_reconciled_only_when_dead_and_stale(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    probe_state = {"state": PROCESS_ALIVE}
    monkeypatch.setattr(
        fleet_store,
        "probe_process",
        lambda _pid, _identity=None: (probe_state["state"], None),
    )
    fleet_store.create_agent(
        _agent_row("orphan-no-lease", status="running", ts=100.0),
        "owner-missing",
        999_999,
    )

    # Dead check is fail-closed: live PID is never interrupted even past deadline.
    clock["now"] = 1_000.0
    live_res = fleet_store.reconcile_stale_owners(now=1_000.0)
    assert live_res["interrupted"] == 0
    assert fleet_store.get_agent("orphan-no-lease")["status"] == "running"

    # Dead PID but still fresh (within deadline): not interrupted yet.
    probe_state["state"] = PROCESS_DEAD
    fresh_res = fleet_store.reconcile_stale_owners(now=200.0)
    assert fresh_res["interrupted"] == 0
    assert fleet_store.get_agent("orphan-no-lease")["status"] == "running"

    # Dead PID AND stale past progress deadline: reconciled cleanly to interrupted.
    stale_res = fleet_store.reconcile_stale_owners(now=1_000.0)
    assert stale_res["interrupted"] == 1
    assert stale_res["reconciled_orphans"] == ["orphan-no-lease"]
    row = fleet_store.get_agent("orphan-no-lease")
    assert row["status"] == "interrupted"
    assert row["finished_ts"] == 1_000.0


def test_genuine_stall_logs_warning_once_across_repeated_status_polls(
    monkeypatch, tmp_path, caplog,
):
    _isolated_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "10")
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    fleet_store.register_owner("owner-live", os.getpid(), 100.0)
    fleet_store.create_agent(
        _agent_row("agent-genuine-stall", ts=100.0),
        "owner-live",
        os.getpid(),
    )
    fleet_store.start_agent("agent-genuine-stall", "owner-live", "wedged tool")

    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)
    # Simulate 20 consecutive /v1/sonder/status polls while the live agent stays stalled.
    for tick in range(20):
        clock["now"] = 120.0 + tick * 2.0
        snap = fleet_store.snapshot(include_finished=False)
        assert snap["stalled_agent_count"] == 1
        assert snap["stalled_agent_total"] == 1
        assert snap["stalled_agents"][0]["id"] == "agent-genuine-stall"
        assert snap["agents"][0]["stalled"] is True

    warnings = _stall_warnings(caplog)
    assert len(warnings) == 1
    assert "agent-genuine-stall" in warnings[0].getMessage()
    assert "no progress" in warnings[0].getMessage()


def test_stall_warning_rearms_after_progress_and_surfaces_distinct_agents(
    monkeypatch, tmp_path, caplog,
):
    _isolated_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "10")
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    fleet_store.register_owner("owner-live", os.getpid(), 100.0)
    fleet_store.create_agent(_agent_row("agent-a", ts=100.0), "owner-live", os.getpid())
    fleet_store.start_agent("agent-a", "owner-live", "step 1")

    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)

    # First stall episode -> warned once.
    clock["now"] = 120.0
    fleet_store.snapshot()
    clock["now"] = 125.0
    fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 1

    # Agent makes real progress -> recovers, then stalls again later -> warns once more.
    clock["now"] = 130.0
    fleet_store.update_agent("agent-a", "owner-live", activity="step 2")
    clock["now"] = 135.0
    assert fleet_store.snapshot()["stalled_agent_count"] == 0
    assert len(_stall_warnings(caplog)) == 1

    clock["now"] = 150.0
    fleet_store.snapshot()
    clock["now"] = 155.0
    fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 2

    # A second distinct stalled agent still warns once while agent-a stays deduplicated.
    clock["now"] = 156.0
    fleet_store.create_agent(_agent_row("agent-b", ts=156.0), "owner-live", os.getpid())
    fleet_store.start_agent("agent-b", "owner-live", "other work")
    clock["now"] = 170.0
    snap = fleet_store.snapshot()
    assert snap["stalled_agent_count"] == 2
    warnings = _stall_warnings(caplog)
    assert len(warnings) == 3
    assert "agent-b" in warnings[-1].getMessage()


def test_stall_warning_optional_rate_limit_interval_env(monkeypatch, tmp_path, caplog):
    _isolated_store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "10")
    monkeypatch.setenv("SONDER_FLEET_STALL_WARN_INTERVAL_SECONDS", "60")
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    fleet_store.register_owner("owner-rate", os.getpid(), 100.0)
    fleet_store.create_agent(_agent_row("agent-rate", ts=100.0), "owner-rate", os.getpid())
    fleet_store.start_agent("agent-rate", "owner-rate", "waiting")
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)

    clock["now"] = 120.0
    fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 1

    # Polls within the 60s window are suppressed.
    for ts in (125.0, 150.0, 179.9):
        clock["now"] = ts
        fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 1

    # Once 60s have elapsed, one rate-limited reminder is logged, then suppressed again.
    clock["now"] = 180.0
    fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 2

    clock["now"] = 200.0
    fleet_store.snapshot()
    assert len(_stall_warnings(caplog)) == 2


def test_stall_warning_dedupe_isolated_per_database_and_clears_on_finish(
    monkeypatch, tmp_path, caplog,
):
    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    for db_name in ("db-one.db", "db-two.db"):
        monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / db_name))
        monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "10")
        monkeypatch.delenv("SONDER_FLEET_STALL_WARN_INTERVAL_SECONDS", raising=False)
        fleet_store.clear_all()
        clock["now"] = 100.0
        fleet_store.register_owner("owner-same", os.getpid(), 100.0)
        fleet_store.create_agent(_agent_row("shared-id", ts=100.0), "owner-same", os.getpid())
        fleet_store.start_agent("shared-id", "owner-same", "working")
        clock["now"] = 120.0
        fleet_store.snapshot()

    # Each distinct database emitted its own fresh warning for "shared-id".
    assert len(_stall_warnings(caplog)) == 2

    # Finishing clears its entry from the dedupe cache.
    fleet_store.finish_agent("shared-id", "owner-same", output="done")
    db_two_key = fleet_store._db_cache_key(str(tmp_path / "db-two.db"))
    assert (db_two_key, "shared-id") not in fleet_store._STALL_WARNED


def test_synthetic_root_with_stalled_active_child_warns_only_for_child_once(
    monkeypatch, tmp_path, caplog,
):
    _isolated_store(monkeypatch, tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])

    root_id = "lane-root-" + "f" * 32
    child_id = "lane-child-stuck"
    _insert_synthetic_interactive_root(root_id, ts=100.0)
    _insert_interactive_child(child_id, root_id, status="running", ts=100.0)

    caplog.set_level(logging.WARNING, logger=fleet_store.logger.name)
    for tick in range(10):
        clock["now"] = 1_000.0 + tick * 5.0
        snap = fleet_store.snapshot()
        assert fleet_store.get_agent(root_id)["status"] == "running"
        assert snap["stalled_agent_count"] == 1
        assert snap["stalled_agent_total"] == 1
        assert [row["id"] for row in snap["stalled_agents"]] == [child_id]

    warnings = _stall_warnings(caplog)
    assert len(warnings) == 1
    assert child_id in warnings[0].getMessage()
    assert root_id not in warnings[0].getMessage()


def test_stall_warning_interval_env_parsing_and_clamping(monkeypatch):
    for raw, expected in (
        ("", 0.0),
        ("0", 0.0),
        ("-10", 0.0),
        ("nan", 0.0),
        ("inf", 0.0),
        ("bogus", 0.0),
        ("45.5", 45.5),
        ("999999", float(24 * 60 * 60)),
    ):
        monkeypatch.setenv("SONDER_FLEET_STALL_WARN_INTERVAL_SECONDS", raw)
        assert fleet_store.stall_warning_interval_seconds() == expected

