import os
import sqlite3
import time

import pytest

import fanout_store as store


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FANOUT_DB", str(tmp_path / "fanout.db"))
    store.reset_schema_cache_for_tests()
    yield tmp_path / "fanout.db"
    store.reset_schema_cache_for_tests()


def test_receipt_lifecycle_is_wal_foreign_key_and_bounded(isolated):
    run = store.create_run("Bearer abcdefghijklmnop and api_key=secret", ["local", "cloud"], request_owner="user")
    assert "secret" not in run["prompt"] and "<redacted>" in run["prompt"]
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    first = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    assert store.record_result(run["id"], first["model"], "worker", "answered", answer="x" * 100_000)["answer"] == "x" * store.MAX_ANSWER_CHARS
    second = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    store.record_result(run["id"], second["model"], "worker", "failed", error="timeout", elapsed_ms=12)
    assert store.get_run(run["id"])["status"] == "completed"
    conn = sqlite3.connect(isolated)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()
    active = store._connect()
    try:
        assert active.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        active.close()


def test_cancel_skips_pending_and_discards_late_answer():
    run = store.create_run("question", ["a", "b"])
    store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    claimed = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    cancelling = store.request_cancel(run["id"])
    assert cancelling["status"] == "cancelled"
    assert store.record_result(run["id"], claimed["model"], "worker", "answered", answer="late") is None
    assert {r["status"] for r in store.list_results(run["id"])} == {"skipped"}


def test_stale_claim_becomes_unknown_and_is_not_replayed():
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "dead", owner_pid=2_147_483_647, lease_seconds=30)
    store.claim_next_result(run["id"], "dead", owner_pid=2_147_483_647, lease_seconds=30)
    assert store.reconcile_stale_runs(now=time.time() + 31) == 1
    assert store.get_run(run["id"])["status"] == "interrupted"
    assert store.list_results(run["id"])[0]["status"] == "unknown"


def test_health_success_resets_failure_and_prune_removes_terminal_receipts():
    bad = store.record_model_health("m", error="429", disabled_until=time.time() + 1)
    assert bad["failure_count"] == 1
    assert store.record_model_health("m", success=True)["failure_count"] == 0
    run = store.create_run("q", ["m"])
    store.claim_run(run["id"], "owner", owner_pid=os.getpid())
    claim = store.claim_next_result(run["id"], "owner", owner_pid=os.getpid())
    store.record_result(run["id"], claim["model"], "owner", "answered")
    result = store.prune(ttl_seconds=60, now=time.time() + 61)
    assert result["runs"] == 1 and store.get_run(run["id"]) is None


def test_resume_requires_explicit_unknown_retry():
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "dead", owner_pid=2_147_483_647)
    store.claim_next_result(run["id"], "dead", owner_pid=2_147_483_647)
    store.reconcile_stale_runs(now=time.time() + 301)
    assert store.resume_run(run["id"]) is None
    resumed = store.resume_run(run["id"], retry_unknown=True)
    assert resumed["status"] == "queued"
    assert store.list_results(run["id"])[0]["status"] == "pending"
