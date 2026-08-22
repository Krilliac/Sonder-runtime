import os
import sqlite3
import subprocess
import sys

import pytest

import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    path = tmp_path / "autopilot.db"
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(path))
    autopilot_store.reset_schema_cache_for_tests()
    yield path
    autopilot_store.reset_schema_cache_for_tests()


def test_run_lifecycle_persists_plan_events_and_exact_counts():
    run = autopilot_store.create_run(
        "Inspect and validate the workspace", max_tasks=7, max_failures=2,
    )
    claimed = autopilot_store.claim_run(run["id"], "owner-a", owner_pid=os.getpid())
    assert claimed["status"] == "planning"

    plan = [{
        "id": "task-01", "title": "Inspect", "instruction": "Read files",
        "kind": "inspect", "status": "pending", "attempts": 0,
        "output": "", "error": "", "history": [],
    }]
    saved = autopilot_store.save_progress(
        run["id"], "owner-a", plan=plan, criteria=["Evidence exists"],
        status="running", phase="execute", cycles_delta=1,
        event_kind="test", event_message="progress persisted",
    )
    assert saved["plan"] == plan
    assert saved["criteria"] == ["Evidence exists"]
    assert saved["cycles"] == 1
    assert saved["adaptive"] is True
    assert saved["max_replans"] == 2
    saved = autopilot_store.save_progress(
        run["id"], "owner-a", checkpoints_delta=1, replans_delta=1,
        event_kind="adaptive_replan", event_message="plan revised",
    )
    assert saved["checkpoints"] == 1
    assert saved["replans"] == 1
    assert autopilot_store.heartbeat(run["id"], "owner-a") is True

    paused = autopilot_store.finish_run(
        run["id"], "owner-a", "paused", summary="bounded stop",
    )
    assert paused["status"] == "paused"
    snap = autopilot_store.snapshot(limit=1)
    assert snap["total_runs"] == 1
    assert snap["resumable_runs"] == 1
    assert any(event["kind"] == "test" for event in autopilot_store.events(run["id"]))


def test_completed_status_requires_durable_host_validation_receipts():
    run = autopilot_store.create_run("do not complete from prose alone")
    owner = "owner-a"
    autopilot_store.claim_run(run["id"], owner, owner_pid=os.getpid())

    # A public repository caller used to be able to bypass the controller and
    # publish this terminal state with no plan or host evidence at all.
    assert autopilot_store.finish_run(run["id"], owner, "completed") is None
    stored = autopilot_store.get_run(run["id"])
    assert stored["status"] == "planning"
    assert autopilot_store.events(run["id"])[-1]["kind"] == "completion_refused"

    plan = [{
        "id": "task-01", "title": "Claimed validation", "kind": "validate",
        "instruction": "run check", "status": "passed", "attempts": 1,
        "output": "PASS", "error": "", "history": [],
        # Model text is not a host receipt and must not make completion legal.
        "host_receipt": {},
    }]
    autopilot_store.save_progress(
        run["id"], owner, plan=plan, criteria=["the check passes"],
    )
    assert autopilot_store.finish_run(run["id"], owner, "completed") is None
    assert autopilot_store.get_run(run["id"])["status"] == "planning"

    plan[0]["host_receipt"] = {
        "tools": ["workspace_run"],
        "mutation_observed": False,
        "validation_attempted": True,
        "validation_passed": True,
    }
    autopilot_store.save_progress(run["id"], owner, plan=plan)
    completed = autopilot_store.finish_run(run["id"], owner, "completed")
    assert completed["status"] == "completed"
    assert autopilot_store.events(run["id"])[-1]["kind"] == "completed"


def test_terminal_event_and_status_roll_back_together_if_event_write_fails(monkeypatch):
    run = autopilot_store.create_run("event ordering survives a write fault")
    owner = "owner-a"
    autopilot_store.claim_run(run["id"], owner, owner_pid=os.getpid())
    before = autopilot_store.events(run["id"])

    def fail_event(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected event write failure")

    monkeypatch.setattr(autopilot_store, "_event", fail_event)
    with pytest.raises(sqlite3.OperationalError, match="event write failure"):
        autopilot_store.finish_run(run["id"], owner, "paused")

    stored = autopilot_store.get_run(run["id"])
    assert stored["status"] == "planning"
    assert autopilot_store.events(run["id"]) == before


def test_active_pause_and_cancel_are_cooperative():
    first = autopilot_store.create_run("pause me")
    autopilot_store.claim_run(first["id"], "owner", owner_pid=os.getpid())
    pause = autopilot_store.request_pause(first["id"])
    assert pause["status"] == "planning"
    assert pause["pause_requested"] is True
    assert autopilot_store.control_flags(first["id"], "owner")["pause"] is True

    second = autopilot_store.create_run("cancel me")
    autopilot_store.claim_run(second["id"], "owner", owner_pid=os.getpid())
    cancel = autopilot_store.request_cancel(second["id"])
    assert cancel["cancel_requested"] is True
    assert autopilot_store.control_flags(second["id"], "owner")["cancel"] is True


def test_ready_cancel_is_terminal_and_cannot_be_claimed():
    run = autopilot_store.create_run("do not run")
    cancelled = autopilot_store.request_cancel(run["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["finished_ts"] is not None
    assert autopilot_store.claim_run(
        run["id"], "late-owner", owner_pid=os.getpid(),
    ) is None


def test_account_owned_runs_are_invisible_and_uncontrollable_cross_account():
    first = autopilot_store.create_run("account one", request_owner="account-one")
    second = autopilot_store.create_run("account two", request_owner="account-two")

    assert autopilot_store.get_run(first["id"], request_owner="account-two") is None
    assert autopilot_store.list_runs(request_owner="account-one") == [
        autopilot_store.get_run(first["id"], request_owner="account-one")
    ]
    assert autopilot_store.request_pause(first["id"], request_owner="account-two") is None
    assert autopilot_store.request_cancel(first["id"], request_owner="account-two") is None
    assert autopilot_store.claim_run(
        first["id"], "wrong-account", owner_pid=os.getpid(), request_owner="account-two",
    ) is None
    assert autopilot_store.get_run(first["id"])["status"] == "ready"
    assert autopilot_store.get_run(second["id"])["status"] == "ready"


def test_dead_local_owner_is_marked_interrupted_and_requires_resume():
    run = autopilot_store.create_run("survive restart")
    autopilot_store.claim_run(run["id"], "dead-owner", owner_pid=2_147_483_647)
    restored = autopilot_store.get_run(run["id"])
    assert restored["status"] == "interrupted"
    assert restored["owner_id"] == ""
    assert "explicit resume" in autopilot_store.events(run["id"])[-1]["message"]


def test_process_liveness_distinguishes_running_and_exited_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert autopilot_store._pid_alive(child.pid) is True
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert autopilot_store._pid_alive(child.pid) is False


def test_second_process_can_request_pause(isolated_autopilot_db):
    run = autopilot_store.create_run("cross-process control")
    autopilot_store.claim_run(run["id"], "owner", owner_pid=os.getpid())
    env = dict(os.environ)
    env["SONDER_AUTOPILOT_DB"] = str(isolated_autopilot_db)
    code = (
        "import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store; "
        "row=autopilot_store.request_pause(%r); "
        "print(row['pause_requested'])" % run["id"]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(autopilot_store.__file__)))),
        env=env, text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    assert autopilot_store.control_flags(run["id"], "owner")["pause"] is True


def test_same_path_database_restore_reinitializes_schema(isolated_autopilot_db, tmp_path):
    """A restore may replace the ledger while this process retains its cache."""
    autopilot_store.create_run("before replacement")
    replacement = tmp_path / "restored-empty.db"
    sqlite3.connect(replacement).close()
    os.replace(replacement, isolated_autopilot_db)

    restored = autopilot_store.create_run("after replacement")

    assert restored["objective"] == "after replacement"
    with sqlite3.connect(isolated_autopilot_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM autopilot_runs").fetchone()[0] == 1


def test_existing_database_is_migrated_without_losing_runs(isolated_autopilot_db):
    conn = sqlite3.connect(isolated_autopilot_db)
    conn.executescript("""
        CREATE TABLE autopilot_runs (
            id TEXT PRIMARY KEY, objective TEXT NOT NULL, project TEXT DEFAULT '',
            tier TEXT NOT NULL, policy TEXT NOT NULL, allow_web INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, phase TEXT NOT NULL, plan_json TEXT NOT NULL DEFAULT '[]',
            criteria_json TEXT NOT NULL DEFAULT '[]', plan_summary TEXT DEFAULT '',
            current_task INTEGER, cycles INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0, max_failures INTEGER NOT NULL DEFAULT 3,
            max_tasks INTEGER NOT NULL DEFAULT 12, owner_id TEXT DEFAULT '',
            owner_pid INTEGER DEFAULT 0, owner_host TEXT DEFAULT '', lease_until REAL,
            pause_requested INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL, finished_ts REAL, summary TEXT DEFAULT '',
            final_report TEXT DEFAULT '', last_error TEXT DEFAULT ''
        );
        INSERT INTO autopilot_runs(
            id, objective, tier, policy, status, phase, created_ts, updated_ts
        ) VALUES ('auto-legacy', 'legacy goal', 'code', 'workspace',
                  'paused', 'paused', 1, 1);
    """)
    conn.commit()
    conn.close()
    autopilot_store.reset_schema_cache_for_tests()

    run = autopilot_store.get_run("auto-legacy")

    assert run["objective"] == "legacy goal"
    assert run["adaptive"] is True
    assert run["checkpoints"] == 0
    assert run["replans"] == 0
    assert run["max_replans"] == 2
