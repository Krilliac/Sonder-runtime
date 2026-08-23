"""Adversarial durability tests for durable agent orchestration.

Covers the failure shapes that only appear under concurrency or partial
failure: duplicate retry dispatch, cancellation arriving inside a transient
retry window, stale/expired retry claims, partial fanout during child
creation, and resuming a master after an unsuccessful retry attempt.
"""
import sqlite3
import subprocess
import threading
import time

import pytest

import master_orchestrator
import server
import sonder_runtime.adapters.persistence.fleet_store as fleet_store


def setup_function():
    master_orchestrator.reset_for_tests()


def _row(agent_id, *, role="agent", parent_id="", task="finish the audit"):
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


def _interrupted_master(agent_id, owner="owner-a", pid=101):
    fleet_store.register_owner(owner, pid, 100.0)
    fleet_store.create_agent(_row(agent_id, role="master"), owner, pid)
    fleet_store.start_agent(agent_id, owner, "running")
    fleet_store.close_owner(owner, "simulated crash")
    return fleet_store.get_agent(agent_id)


# --- retry idempotency fence (duplicate execution) -------------------------


def test_retry_lease_is_exclusive_until_released(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    source = _interrupted_master("master-x")
    assert source["status"] == "interrupted"

    lease = fleet_store.acquire_retry_lease("master-x")
    duplicate = fleet_store.acquire_retry_lease("master-x")
    released = fleet_store.release_retry_lease("master-x", lease["token"])
    reacquired = fleet_store.acquire_retry_lease("master-x")

    assert lease and lease["token"]
    assert duplicate is None
    assert released is True
    assert reacquired and reacquired["token"] != lease["token"]


def test_retry_lease_concurrent_acquisition_has_single_winner(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-race")
    results = []
    barrier = threading.Barrier(6)

    def contender():
        barrier.wait()
        results.append(fleet_store.acquire_retry_lease("master-race"))

    threads = [threading.Thread(target=contender) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [item for item in results if item]
    assert len(winners) == 1


def test_retry_lease_expires_and_is_reacquirable(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-stale")

    lease = fleet_store.acquire_retry_lease(
        "master-stale", lease_seconds=5, now=1000.0,
    )
    still_held = fleet_store.acquire_retry_lease("master-stale", now=1002.0)
    expired = fleet_store.acquire_retry_lease("master-stale", now=1006.0)

    assert lease and lease["expires_ts"] == 1005.0
    assert still_held is None
    assert expired and expired["token"] != lease["token"]


def test_retry_lease_refused_while_retry_master_active_and_after_success(
    monkeypatch, tmp_path,
):
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-src")
    fleet_store.register_owner("owner-new", 202, 200.0)
    retry = _row("master-src-retry", role="master", task="retry task")
    retry["retry_of"] = "master-src"
    fleet_store.create_agent(retry, "owner-new", 202)

    while_active = fleet_store.acquire_retry_lease("master-src")
    fleet_store.start_agent("master-src-retry", "owner-new", "retrying")
    fleet_store.finish_agent("master-src-retry", "owner-new", output="recovered")
    after_success = fleet_store.acquire_retry_lease("master-src")
    source = fleet_store.get_agent("master-src")

    assert while_active is None
    assert source["status"] == "retried"
    assert source["retried_by"] == "master-src-retry"
    assert after_success is None


def test_release_never_clears_a_recorded_successful_retry(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-keep")
    fleet_store.register_owner("owner-new", 202, 200.0)
    lease = fleet_store.acquire_retry_lease("master-keep")
    retry = _row("master-keep-retry", role="master", task="retry task")
    retry["retry_of"] = "master-keep"
    fleet_store.create_agent(retry, "owner-new", 202)
    fleet_store.start_agent("master-keep-retry", "owner-new", "retrying")
    fleet_store.finish_agent("master-keep-retry", "owner-new", output="done")

    released = fleet_store.release_retry_lease("master-keep", lease["token"])
    source = fleet_store.get_agent("master-keep")

    assert released is False
    assert source["retried_by"] == "master-keep-retry"


# --- resume after an unsuccessful retry ------------------------------------


def test_failed_retry_clears_pending_claim_for_the_next_attempt(
    monkeypatch, tmp_path,
):
    """Simulates a dispatcher that crashed after claiming and dispatching."""
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-again")
    lease = fleet_store.acquire_retry_lease("master-again")
    assert lease
    fleet_store.register_owner("owner-new", 202, 200.0)
    retry = _row("master-again-retry", role="master", task="retry task")
    retry["retry_of"] = "master-again"
    fleet_store.create_agent(retry, "owner-new", 202)
    fleet_store.start_agent("master-again-retry", "owner-new", "retrying")

    fleet_store.finish_agent("master-again-retry", "owner-new", error="worker died")
    source = fleet_store.get_agent("master-again")
    second_attempt = fleet_store.acquire_retry_lease("master-again")

    assert source["status"] == "interrupted"
    assert source["retried_by"] == ""
    assert second_attempt is not None


def test_master_retry_end_to_end_duplicate_fence(monkeypatch):
    """server.master_retry refuses to double-dispatch a persisted master."""
    fleet_store.register_owner("owner-r", 11, 1.0)
    fleet_store.create_agent(_row("master-dup", role="master"), "owner-r", 11)
    fleet_store.start_agent("master-dup", "owner-r", "running")
    fleet_store.close_owner("owner-r", "simulated crash")
    fleet_store.register_owner("owner-r2", 12, 2.0)
    active_retry = _row("master-dup-retry", role="master", task="retry task")
    active_retry["retry_of"] = "master-dup"
    fleet_store.create_agent(active_retry, "owner-r2", 12)
    calls = []
    monkeypatch.setattr(
        server, "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "retry complete",
    )

    refused = server.master_retry("master-dup")

    assert refused.startswith("ERROR:")
    assert "retry in flight" in refused
    assert calls == []

    fleet_store.start_agent("master-dup-retry", "owner-r2", "retrying")
    fleet_store.finish_agent("master-dup-retry", "owner-r2", error="boom")

    dispatched = server.master_retry("master-dup")
    source = fleet_store.get_agent("master-dup")

    assert "persisted master retry" in dispatched
    assert "retry complete" in dispatched
    assert len(calls) == 1
    assert calls[0]["retry_of"] == "master-dup"
    # The pending dispatch claim is always released after dispatch; the durable
    # fence from here on is the (mocked-away) retry master row itself.
    assert source["retried_by"] == ""


# --- partial fanout --------------------------------------------------------


def test_partial_fanout_child_creation_failure_cancels_created_children(
    monkeypatch,
):
    original = fleet_store.create_agent
    counter = {"agents": 0}

    def flaky_create(row, owner_id, owner_pid, **kwargs):
        if row.get("role") == "agent":
            counter["agents"] += 1
            if counter["agents"] == 3:
                raise sqlite3.OperationalError("database is locked")
        return original(row, owner_id, owner_pid, **kwargs)

    monkeypatch.setattr(fleet_store, "create_agent", flaky_create)
    worker_calls = []
    started = []

    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=lambda prompt: worker_calls.append(prompt) or "unused",
        audit_fn=lambda prompt: "must not run",
        agents=3,
        _on_started=lambda payload: started.append(payload),
    )
    snap = master_orchestrator.snapshot()
    children = [row for row in snap["agents"] if row["role"] == "agent"]
    master = next(row for row in snap["agents"] if row["role"] == "master")

    assert result["output"].startswith(
        "ERROR: fleet startup failed after queueing 2 of 3"
    )
    assert worker_calls == []
    assert started and started[0]["output"] == result["output"]
    assert len(children) == 2
    assert {row["status"] for row in children} == {"cancelled"}
    assert master["status"] == "failed"


def test_abandoned_background_startup_is_cancelled_not_orphaned(monkeypatch):
    """A startup-timeout fleet must not keep running where no caller can see it."""
    cancelled = []

    def slow_run_delegated(task, **kwargs):
        time.sleep(0.5)
        kwargs["_on_started"]({
            "mode": "delegated", "master_id": "master-late",
            "agents": [], "worker_slots": 1, "outputs": [], "output": "RUNNING",
        })
        return {}

    monkeypatch.setattr(master_orchestrator, "run_delegated", slow_run_delegated)
    monkeypatch.setattr(
        master_orchestrator, "request_cancel",
        lambda selector: cancelled.append(selector) or {"agents": []},
    )

    with pytest.raises(RuntimeError, match="cancelled instead of running as an orphan"):
        master_orchestrator.start_delegated(
            "late task", worker_fn=lambda prompt: "x",
            audit_fn=lambda prompt: "y", startup_timeout=0.1,
        )
    deadline = time.time() + 3
    while not cancelled and time.time() < deadline:
        time.sleep(0.02)

    assert cancelled == ["master-late"]


# --- transient retry classification and cancellation-during-retry ----------


def test_classify_worker_error_vocabulary():
    class FakeHttpError(Exception):
        def __init__(self, code):
            super().__init__("HTTP Error %d" % code)
            self.code = code

    cases = [
        (TimeoutError("read timed out"), "timeout"),
        (subprocess.TimeoutExpired(cmd="x", timeout=1), "timeout"),
        (ConnectionRefusedError("refused"), "unavailable"),
        (ConnectionResetError("reset"), "unavailable"),
        (FakeHttpError(429), "throttled"),
        (FakeHttpError(503), "unavailable"),
        (FakeHttpError(404), "request_rejected"),
        (RuntimeError("Connection reset by peer"), "unavailable"),
        (RuntimeError("model call timed out after 150s"), "timeout"),
        (RuntimeError("HTTP 429 Too Many Requests"), "throttled"),
        (OSError("unusual io fault"), "transport"),
        (ValueError("model returned malformed JSON"), "unknown"),
        (RuntimeError("repository worker scope mismatch"), "unknown"),
    ]
    for exc, expected in cases:
        assert master_orchestrator.classify_worker_error(exc) == expected, exc

    assert master_orchestrator.TRANSIENT_FAILURE_CLASSES <= set(
        # Traceability: the fleet lane reuses the fanout receipt vocabulary.
        __import__(
            "sonder_runtime.adapters.persistence.fanout_store",
            fromlist=["FAILURE_CLASSES"],
        ).FAILURE_CLASSES
    )


def test_transient_worker_error_is_retried_once_then_succeeds():
    attempts = []

    def flaky(prompt):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise TimeoutError("read timed out")
        return "recovered result"

    result = master_orchestrator.run_delegated(
        "compare options", worker_fn=flaky,
        audit_fn=lambda prompt: "merged", agents=1,
    )
    snap = master_orchestrator.snapshot()
    child = next(row for row in snap["agents"] if row["role"] == "agent")

    assert result["output"] == "merged"
    assert len(attempts) == 2
    assert child["status"] == "done"


def test_permanent_worker_error_is_not_retried_and_carries_failure_class():
    attempts = []

    def broken(prompt):
        attempts.append(prompt)
        raise ValueError("model returned malformed JSON")

    result = master_orchestrator.run_delegated(
        "compare options", worker_fn=broken,
        audit_fn=lambda prompt: "must not run", agents=1,
    )
    snap = master_orchestrator.snapshot()
    child = next(row for row in snap["agents"] if row["role"] == "agent")

    assert "all delegated workers failed" in result["output"]
    assert len(attempts) == 1
    assert child["status"] == "failed"
    assert child["error"].startswith("[unknown] ")


def test_cancellation_during_transient_retry_window_prevents_second_attempt():
    attempts = []

    def flaky_then_cancelled(prompt):
        attempts.append(prompt)
        agent_id = master_orchestrator._WORKER_LOCAL.agent_id
        master_orchestrator.request_cancel(agent_id)
        raise TimeoutError("read timed out")

    result = master_orchestrator.run_delegated(
        "compare options", worker_fn=flaky_then_cancelled,
        audit_fn=lambda prompt: "must not run", agents=1,
    )
    snap = master_orchestrator.snapshot()
    child = next(row for row in snap["agents"] if row["role"] == "agent")

    assert len(attempts) == 1, "cancellation must suppress the retry attempt"
    assert child["status"] == "cancelled"
    assert result["outputs"] == []


def test_status_reports_retry_lineage_for_recovery_evidence(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    _interrupted_master("master-lineage")
    fleet_store.register_owner("owner-new", 202, 200.0)
    retry = _row("master-lineage-retry", role="master", task="retry task")
    retry["retry_of"] = "master-lineage"
    fleet_store.create_agent(retry, "owner-new", 202)
    fleet_store.start_agent("master-lineage-retry", "owner-new", "retrying")
    fleet_store.finish_agent("master-lineage-retry", "owner-new", output="recovered")

    rendered = master_orchestrator.format_snapshot(master_orchestrator.snapshot())

    assert "retry of: master-lineage" in rendered
    assert "retried by: master-lineage-retry" in rendered


def test_transient_retry_budget_is_operator_boundable(monkeypatch):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "0")
    attempts = []

    def flaky(prompt):
        attempts.append(prompt)
        raise TimeoutError("read timed out")

    master_orchestrator.run_delegated(
        "compare options", worker_fn=flaky,
        audit_fn=lambda prompt: "must not run", agents=1,
    )

    assert len(attempts) == 1

    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "junk")
    assert master_orchestrator.worker_transient_retries() == 1
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "99")
    assert master_orchestrator.worker_transient_retries() == 3
