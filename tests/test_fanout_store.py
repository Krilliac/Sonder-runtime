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


def test_sealed_execution_prompt_is_not_exposed_by_receipt_readers():
    run = store.create_run("private prompt", ["local"], execution_prompt_ciphertext="ciphertext")

    assert "execution_prompt_ciphertext" not in store.get_run(run["id"])
    assert "execution_prompt_ciphertext" not in store.list_runs()[0]
    assert store.execution_prompt_ciphertext(run["id"]) == "ciphertext"


def test_schema_migration_scrubs_pre_vault_prompt_and_digest(isolated):
    run = store.create_run("private legacy prompt", ["local"])
    conn = sqlite3.connect(isolated)
    conn.execute("UPDATE fanout_runs SET prompt=?, prompt_sha256=?, execution_prompt_ciphertext='' WHERE id=?",
                 ("private legacy prompt", "guessable-digest", run["id"]))
    conn.commit(); conn.close()
    store.reset_schema_cache_for_tests()

    migrated = store.get_run(run["id"])
    assert migrated["prompt"] == "legacy-fanout-prompt:redacted"
    conn = sqlite3.connect(isolated)
    raw = conn.execute("SELECT prompt, prompt_sha256 FROM fanout_runs WHERE id=?", (run["id"],)).fetchone()
    conn.close()
    assert raw == ("legacy-fanout-prompt:redacted", "")


def test_execution_ciphertext_is_bounded_without_truncation():
    with pytest.raises(ValueError, match="sealed prompt exceeds"):
        store.create_run("question", ["local"], execution_prompt_ciphertext="x" * 64_001)


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


def test_nonavailability_health_error_does_not_increase_backoff_counter():
    first = store.record_model_health("m", error="timeout", disabled_until=time.time() + 1)
    assert first["failure_count"] == 1
    assert first["availability_failure_count"] == 1
    prompt_error = store.record_model_health(
        "m", error="request rejected", counts_toward_backoff=False,
    )
    assert prompt_error["failure_count"] == 2
    assert prompt_error["availability_failure_count"] == 1


def test_health_migration_starts_availability_counter_at_zero(tmp_path, monkeypatch):
    database = tmp_path / "legacy-fanout.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE model_health (model TEXT PRIMARY KEY, model_class TEXT NOT NULL DEFAULT '', failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', disabled_until REAL, last_success_ts REAL, updated_ts REAL NOT NULL)")
    conn.execute("INSERT INTO model_health(model, failure_count, updated_ts) VALUES('m', 9, 1)")
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "database_path", lambda: str(database))
    store.reset_schema_cache_for_tests()

    health = store.get_model_health("m")

    assert health["failure_count"] == 9
    assert health["availability_failure_count"] == 0


def test_resume_requires_explicit_unknown_retry():
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "dead", owner_pid=2_147_483_647)
    store.claim_next_result(run["id"], "dead", owner_pid=2_147_483_647)
    store.reconcile_stale_runs(now=time.time() + 301)
    assert store.resume_run(run["id"]) is None
    resumed = store.resume_run(run["id"], retry_unknown=True)
    assert resumed["status"] == "queued"
    assert store.list_results(run["id"])[0]["status"] == "pending"


def test_lease_transfer_marks_old_inflight_result_unknown(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "old", owner_pid=os.getpid(), lease_seconds=30)
    store.claim_next_result(run["id"], "old", owner_pid=os.getpid(), lease_seconds=30)
    now[0] += 31

    assert store.claim_run(run["id"], "new", owner_pid=os.getpid())
    assert store.list_results(run["id"])[0]["status"] == "unknown"
    assert store.record_result(run["id"], "a", "old", "answered", answer="late") is None


def test_request_timeout_lease_has_completion_margin(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    run = store.create_run("question", ["a"])
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid(), lease_seconds=360)
    claim = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid(), lease_seconds=360)
    now[0] += 301  # A 300-second request still has time to commit its receipt.
    assert store.record_result(run["id"], claim["model"], "worker", "answered", answer="done")


def test_redactor_covers_quoted_json_and_spaced_credentials():
    run = store.create_run(
        '{"api_key": "supersecret123", "token": \'secret value here\'}', ["a"],
    )

    assert "supersecret123" not in run["prompt"]
    assert "secret value here" not in run["prompt"]
