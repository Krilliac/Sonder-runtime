import os
import subprocess
import sys

import pytest

import autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    path = tmp_path / "autopilot.db"
    monkeypatch.setenv("TRILOBITE_AUTOPILOT_DB", str(path))
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
    assert autopilot_store.heartbeat(run["id"], "owner-a") is True

    paused = autopilot_store.finish_run(
        run["id"], "owner-a", "paused", summary="bounded stop",
    )
    assert paused["status"] == "paused"
    snap = autopilot_store.snapshot(limit=1)
    assert snap["total_runs"] == 1
    assert snap["resumable_runs"] == 1
    assert any(event["kind"] == "test" for event in autopilot_store.events(run["id"]))


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
    env["TRILOBITE_AUTOPILOT_DB"] = str(isolated_autopilot_db)
    code = (
        "import autopilot_store; "
        "row=autopilot_store.request_pause(%r); "
        "print(row['pause_requested'])" % run["id"]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=os.path.dirname(autopilot_store.__file__),
        env=env, text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    assert autopilot_store.control_flags(run["id"], "owner")["pause"] is True
