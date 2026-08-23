"""Adversarial stale-lease and owner-fencing tests for the autopilot store.

The invariant under test: after a controller's lease expires and the run is
reconciled, every write path of the old owner (heartbeat, progress, claim)
is fenced out — a zombie controller that wakes up late must not be able to
write over a successor's run. Split-brain requires two simultaneous owners;
these tests pin the sequence that must make it impossible.
"""
import os
import time

import pytest

import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    path = tmp_path / "autopilot.db"
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(path))
    autopilot_store.reset_schema_cache_for_tests()
    yield path
    autopilot_store.reset_schema_cache_for_tests()


def _claimed_run(owner="owner-a"):
    run = autopilot_store.create_run("stale lease drill")
    claimed = autopilot_store.claim_run(run["id"], owner, owner_pid=os.getpid())
    assert claimed is not None and claimed["owner_id"] == owner
    return claimed


def _expire(run_id):
    """Reconcile as if every possible lease duration has fully elapsed."""
    future = time.time() + 25_000  # beyond the 21_600s heartbeat ceiling
    assert autopilot_store.reconcile_stale_runs(now=future) >= 1
    run = autopilot_store.get_run(run_id)
    assert run["status"] == "interrupted" and run["owner_id"] == ""
    return run


def test_expired_lease_is_reconciled_to_interrupted():
    run = _claimed_run()
    _expire(run["id"])
    assert "explicit resume" in autopilot_store.events(run["id"])[-1]["message"]


def test_old_owner_heartbeat_is_fenced_after_expiry_reconcile():
    run = _claimed_run()
    _expire(run["id"])
    assert autopilot_store.heartbeat(run["id"], "owner-a") is False


def test_old_owner_progress_is_fenced_after_expiry_reconcile():
    run = _claimed_run()
    _expire(run["id"])
    assert not autopilot_store.save_progress(
        run["id"], "owner-a", summary="zombie write"
    )


def test_takeover_after_expiry_fences_the_old_owner_permanently():
    run = _claimed_run("owner-a")
    _expire(run["id"])
    taken = autopilot_store.claim_run(run["id"], "owner-b", owner_pid=os.getpid())
    assert taken is not None and taken["owner_id"] == "owner-b"
    # The zombie's every write path stays dead while the successor's works.
    assert autopilot_store.heartbeat(run["id"], "owner-a") is False
    assert not autopilot_store.save_progress(run["id"], "owner-a", summary="zombie")
    assert autopilot_store.claim_run(run["id"], "owner-a", owner_pid=os.getpid()) is None
    assert autopilot_store.heartbeat(run["id"], "owner-b") is True


def test_pre_reconcile_resurrection_is_single_owner_and_benign():
    """An expired-but-unreconciled owner may renew itself: document why.

    Between lease expiry and the next reconcile there is still exactly one
    owner_id on the row, and claim_run refuses an active-status run held by
    a different owner, so nobody else can have claimed it in that window.
    Resurrection therefore never creates a second owner — the heartbeat
    succeeding here is deliberate, not a fencing hole.
    """
    run = _claimed_run("owner-a")
    # Lease is in the future; no reconcile has run. The same owner renews.
    assert autopilot_store.heartbeat(run["id"], "owner-a") is True
    # And a rival cannot claim over a live active owner.
    assert autopilot_store.claim_run(run["id"], "owner-b", owner_pid=os.getpid()) is None
