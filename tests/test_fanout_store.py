import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import fanout_store as store
import server


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FANOUT_DB", str(tmp_path / "fanout.db"))
    store.reset_schema_cache_for_tests()
    yield tmp_path / "fanout.db"
    store.reset_schema_cache_for_tests()


def test_receipt_lifecycle_is_wal_foreign_key_and_explicitly_bounded(isolated):
    run = store.create_run("Bearer abcdefghijklmnop and api_key=secret", ["local", "cloud"], request_owner="user")
    assert run["prompt"] == "sealed-fanout-prompt:redacted"
    assert run["prompt_sha256"] == ""
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    first = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    result = store.record_result(run["id"], first["model"], "worker", "answered", answer="x" * 100_000)
    assert result["answer"] == "x" * store.MAX_ANSWER_CHARS
    assert result["answer_chars"] == 100_000
    assert result["answer_truncation_known"] == 1
    assert result["answer_truncated"] == 1
    assert not result["answer"].endswith("...")
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


def test_recent_run_summaries_are_owner_scoped_and_never_return_sensitive_rows():
    first = store.create_run(
        "private prompt alpha", ["local-a", "local-b"],
        request_owner="owner-a", scope="local", execution_prompt_ciphertext="cipher-a",
    )
    second = store.create_run(
        "private prompt beta", ["cloud-a"],
        request_owner="owner-b", scope="cloud", execution_prompt_ciphertext="cipher-b",
    )

    summaries = store.recent_run_summaries(request_owner="owner-a")

    assert [row["run_id"] for row in summaries] == [first["id"]]
    row = summaries[0]
    assert row["models_selected"] == 2
    assert row["models_answered"] == row["models_failed"] == row["models_unknown"] == row["models_running"] == row["models_skipped"] == 0
    assert row["models_pending"] == 2
    assert isinstance(row["total_elapsed_ms"], int)
    assert row["total_elapsed_ms"] >= 0
    assert set(row) == {
        "run_id", "status", "scope", "created_ts", "updated_ts", "finished_ts",
        "models_selected", "models_answered", "models_failed", "models_unknown", "models_pending", "models_running", "models_skipped",
        "total_elapsed_ms",
    }
    rendered = repr(summaries)
    for secret in ("private prompt", "cipher", "owner-a", "models_json", "prompt_sha256"):
        assert secret not in rendered
    assert second["id"] not in rendered


def test_recent_run_summaries_reconcile_stale_receipts_before_projecting(monkeypatch):
    run = store.create_run("private prompt", ["local"], request_owner="owner-a")
    reconciled = []
    monkeypatch.setattr(store, "reconcile_stale_runs", lambda: reconciled.append(True) or 0)

    summaries = store.recent_run_summaries(request_owner="owner-a", limit=1)

    assert reconciled == [True]
    assert summaries[0]["run_id"] == run["id"]


def test_recent_run_summaries_keep_terminal_duration_stable(monkeypatch):
    run = store.create_run("private prompt", ["local"], request_owner="owner-a")
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE fanout_runs SET status='completed', created_ts=?, finished_ts=? WHERE id=?",
            (100.0, 104.25, run["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(store.time, "time", lambda: 999.0)

    summaries = store.recent_run_summaries(request_owner="owner-a", limit=1)

    assert summaries[0]["total_elapsed_ms"] == 4250


def test_schema_migration_scrubs_pre_vault_prompt_and_digest(isolated):
    run = store.create_run("private legacy prompt", ["local"])
    conn = sqlite3.connect(isolated)
    conn.execute("UPDATE fanout_runs SET prompt=?, prompt_sha256=?, execution_prompt_ciphertext='' WHERE id=?",
                 ("private legacy prompt", "guessable-digest", run["id"]))
    conn.commit(); conn.close()
    store.reset_schema_cache_for_tests()

    migrated = store.get_run(run["id"])
    assert migrated["prompt"] == "sealed-fanout-prompt:redacted"
    conn = sqlite3.connect(isolated)
    raw = conn.execute("SELECT prompt, prompt_sha256 FROM fanout_runs WHERE id=?", (run["id"],)).fetchone()
    conn.close()
    assert raw == ("sealed-fanout-prompt:redacted", "")


def test_schema_migration_scrubs_vault_backed_legacy_prompt_and_digest(isolated):
    run = store.create_run(
        "private current prompt", ["local"], execution_prompt_ciphertext="ciphertext"
    )
    conn = sqlite3.connect(isolated)
    conn.execute(
        "UPDATE fanout_runs SET prompt=?, prompt_sha256=? WHERE id=?",
        ("private current prompt", "guessable-digest", run["id"]),
    )
    conn.commit(); conn.close()
    store.reset_schema_cache_for_tests()

    # Recovery still has the sealed execution payload, while the on-disk
    # receipt no longer retains a readable prompt or a dictionary-attack aid.
    assert store.execution_prompt_ciphertext(run["id"]) == "ciphertext"
    with sqlite3.connect(isolated) as migrated:
        raw = migrated.execute(
            "SELECT prompt, prompt_sha256 FROM fanout_runs WHERE id=?", (run["id"],)
        ).fetchone()
    assert raw == ("sealed-fanout-prompt:redacted", "")


def test_result_usage_columns_migrate_and_store_only_bounded_scalars(tmp_path, monkeypatch):
    database = tmp_path / "legacy-fanout.db"
    conn = sqlite3.connect(database)
    conn.executescript("""
        CREATE TABLE fanout_runs (
            id TEXT PRIMARY KEY, request_owner TEXT NOT NULL DEFAULT '', request_role TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, models_json TEXT NOT NULL,
            execution_prompt_ciphertext TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'local',
            cloud_opt_in INTEGER NOT NULL DEFAULT 0, limits_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, owner_id TEXT NOT NULL DEFAULT '',
            owner_pid INTEGER NOT NULL DEFAULT 0, owner_host TEXT NOT NULL DEFAULT '', lease_until REAL,
            created_ts REAL NOT NULL, updated_ts REAL NOT NULL, finished_ts REAL
        );
        CREATE TABLE fanout_results (
            run_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner_id TEXT NOT NULL DEFAULT '', owner_pid INTEGER NOT NULL DEFAULT 0,
            owner_host TEXT NOT NULL DEFAULT '', lease_until REAL, answer TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER, retry_after_ts REAL, started_ts REAL,
            finished_ts REAL, updated_ts REAL NOT NULL, PRIMARY KEY(run_id, model)
        );
    """)
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "database_path", lambda: str(database))
    store.reset_schema_cache_for_tests()

    run = store.create_run("question", ["model"])
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    claimed = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    recorded = store.record_result(
        run["id"], claimed["model"], "worker", "answered", answer="answer",
        answer_chars=999_999, thinking_chars="42", done_reason="provider-specific-value",
    )

    assert recorded["answer_chars"] == 999_999
    assert recorded["answer_truncation_known"] == 1
    assert recorded["answer_truncated"] == 0
    assert recorded["thinking_chars"] == 42
    assert recorded["done_reason"] == "other"
    conn = sqlite3.connect(database)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fanout_results)")}
    conn.close()
    assert {
        "answer_chars", "answer_truncated", "answer_truncation_known",
        "thinking_chars", "done_reason", "failure_class",
    } <= columns
    assert recorded["failure_class"] is None


def test_failure_class_migration_keeps_legacy_receipts_unclassified(tmp_path, monkeypatch):
    database = tmp_path / "legacy-fanout.db"
    conn = sqlite3.connect(database)
    conn.executescript(store._SCHEMA.replace(" failure_class TEXT,", ""))
    conn.execute(
        "INSERT INTO fanout_runs(id,prompt,prompt_sha256,models_json,status,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?)",
        ("legacy", "legacy", "", '["model"]', "completed", 1.0, 1.0),
    )
    conn.execute(
        "INSERT INTO fanout_results(run_id,model,status,error,updated_ts) VALUES(?,?,?,?,?)",
        ("legacy", "model", "failed", "historic failure", 1.0),
    )
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "database_path", lambda: str(database))
    store.reset_schema_cache_for_tests()

    result = store.list_results("legacy")[0]

    assert result["failure_class"] is None
    with sqlite3.connect(database) as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(fanout_results)")}
    assert "failure_class" in columns


def test_legacy_answer_receipt_does_not_guess_truncation_from_exact_storage_limit(tmp_path, monkeypatch):
    database = tmp_path / "legacy-fanout.db"
    conn = sqlite3.connect(database)
    conn.executescript(store._SCHEMA.replace(
        "answer_chars INTEGER NOT NULL DEFAULT 0, answer_truncated INTEGER NOT NULL DEFAULT 0,\n answer_truncation_known INTEGER NOT NULL DEFAULT 0, thinking_chars INTEGER NOT NULL DEFAULT 0,\n done_reason TEXT NOT NULL DEFAULT '',\n ",
        "",
    ))
    conn.execute(
        "INSERT INTO fanout_runs(id,prompt,prompt_sha256,models_json,status,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?)",
        ("legacy", "legacy", "", '["model"]', "completed", 1.0, 1.0),
    )
    conn.execute(
        "INSERT INTO fanout_results(run_id,model,status,answer,updated_ts) VALUES(?,?,?,?,?)",
        ("legacy", "model", "answered", "x" * store.MAX_ANSWER_CHARS, 1.0),
    )
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "database_path", lambda: str(database))
    store.reset_schema_cache_for_tests()

    result = store.list_results("legacy")[0]

    assert result["answer"] == "x" * store.MAX_ANSWER_CHARS
    assert result["answer_chars"] == store.MAX_ANSWER_CHARS
    assert result["answer_truncation_known"] == 0
    assert result["answer_truncated"] == 0


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
    result = store.list_results(run["id"])[0]
    assert result["status"] == "unknown"
    assert result["failure_class"] == "execution_uncertain"
    assert result["retry_after_ts"] is None


def test_stale_queued_run_becomes_resumable_without_replaying_any_call():
    run = store.create_run("question", ["a"])
    created = run["created_ts"]

    assert store.reconcile_stale_runs(
        now=created + store.QUEUED_DISPATCH_GRACE_SECONDS - 1,
    ) == 0
    assert store.get_run(run["id"])["status"] == "queued"

    assert store.reconcile_stale_runs(
        now=created + store.QUEUED_DISPATCH_GRACE_SECONDS,
    ) == 1
    assert store.get_run(run["id"])["status"] == "interrupted"
    result = store.list_results(run["id"])[0]
    assert result["status"] == "skipped"
    assert result["error"] == "worker did not begin; safe explicit resume required"

    resumed = store.resume_run(run["id"])
    assert resumed["status"] == "queued"
    assert store.list_results(run["id"])[0]["status"] == "pending"


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


def _force_terminal(run_id, *, updated_ts):
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE fanout_runs SET status='completed', finished_ts=?, updated_ts=? WHERE id=?",
            (updated_ts, updated_ts, run_id),
        )
        conn.execute("UPDATE fanout_events SET ts=? WHERE run_id=?", (updated_ts, run_id))
        conn.commit()
    finally:
        conn.close()


def _age_health(model, *, updated_ts):
    conn = store._connect()
    try:
        conn.execute("UPDATE model_health SET updated_ts=? WHERE model=?", (updated_ts, model))
        conn.commit()
    finally:
        conn.close()


def _clear_retention_throttle():
    getattr(store, "_LAST_PRUNE_TS", {}).clear()


def test_create_run_expires_stale_terminal_receipts_and_idle_health():
    stale_ts = time.time() - store.DEFAULT_RETENTION_TTL_SECONDS - 60
    old = store.create_run("expired question", ["m"])
    _force_terminal(old["id"], updated_ts=stale_ts)
    store.record_model_health("idle-model", error="timeout")
    store.record_model_health(
        "cooling-model", error="timeout", disabled_until=time.time() + 3600,
    )
    _age_health("idle-model", updated_ts=stale_ts)
    _age_health("cooling-model", updated_ts=stale_ts)
    _clear_retention_throttle()

    fresh = store.create_run("live question", ["m"])

    listed = {row["id"] for row in store.list_runs(limit=50)}
    assert fresh["id"] in listed
    assert old["id"] not in listed
    assert store.events(old["id"]) == []
    assert store.get_model_health("idle-model") is None
    # An active cooldown is operational state, not history: it must survive
    # expiry so an unhealthy target is not immediately re-probed.
    assert store.get_model_health("cooling-model") is not None


def test_read_path_reconciliation_prunes_at_most_once_per_interval():
    keep = store.create_run("live question", ["m"])
    first_old = store.create_run("first expired", ["m"])
    stale_ts = time.time() - store.DEFAULT_RETENTION_TTL_SECONDS - 60
    _force_terminal(first_old["id"], updated_ts=stale_ts)
    _clear_retention_throttle()

    assert store.get_run(keep["id"])["status"] == "queued"
    assert store.get_run(first_old["id"]) is None

    second_old = store.create_run("second expired", ["m"])
    _force_terminal(second_old["id"], updated_ts=stale_ts)

    # Within the throttle interval the read path must not pay for another
    # write transaction, so the second stale receipt temporarily survives.
    assert store.get_run(keep["id"])["status"] == "queued"
    assert store.get_run(second_old["id"]) is not None

    _clear_retention_throttle()
    assert store.get_run(keep["id"])["status"] == "queued"
    assert store.get_run(second_old["id"]) is None


def test_retention_ttl_env_contract(monkeypatch):
    assert store.retention_ttl_seconds() == store.DEFAULT_RETENTION_TTL_SECONDS
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", "junk")
    assert store.retention_ttl_seconds() == store.DEFAULT_RETENTION_TTL_SECONDS
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", "-5")
    assert store.retention_ttl_seconds() == store.DEFAULT_RETENTION_TTL_SECONDS
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", "60")
    assert store.retention_ttl_seconds() == store.MIN_RETENTION_TTL_SECONDS
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", str(400 * 86_400))
    assert store.retention_ttl_seconds() == store.MAX_RETENTION_TTL_SECONDS
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", "0")
    assert store.retention_ttl_seconds() == 0


def test_zero_retention_disables_automatic_expiry(monkeypatch):
    monkeypatch.setenv("SONDER_FANOUT_TTL_SECONDS", "0")
    old = store.create_run("kept question", ["m"])
    _force_terminal(old["id"], updated_ts=time.time() - 400 * 86_400)
    _clear_retention_throttle()

    store.create_run("live question", ["m"])

    assert store.get_run(old["id"]) is not None


def test_automatic_expiry_never_touches_leased_active_runs():
    run = store.create_run("active question", ["m"])
    assert store.claim_run(run["id"], "owner", owner_pid=os.getpid())
    stale_ts = time.time() - store.DEFAULT_RETENTION_TTL_SECONDS - 60
    conn = store._connect()
    try:
        # Age only the bookkeeping timestamp; the lease remains live, exactly
        # like a long-queued durable run that has just been picked back up.
        conn.execute("UPDATE fanout_runs SET updated_ts=? WHERE id=?", (stale_ts, run["id"]))
        conn.commit()
    finally:
        conn.close()
    _clear_retention_throttle()

    store.create_run("live question", ["m"])

    assert store.get_run(run["id"])["status"] == "running"


def test_schema_migration_is_safe_across_two_processes(tmp_path):
    database = tmp_path / "legacy-fanout.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE model_health (model TEXT PRIMARY KEY, model_class TEXT NOT NULL DEFAULT '', failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', disabled_until REAL, last_success_ts REAL, updated_ts REAL NOT NULL)")
    conn.commit(); conn.close()
    env = os.environ.copy()
    env["SONDER_FANOUT_DB"] = str(database)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    script = (
        "import fanout_store as s; "
        "conn=s._connect(); "
        "assert 'availability_failure_count' in "
        "{row['name'] for row in conn.execute('PRAGMA table_info(model_health)')}; "
        "conn.close()"
    )
    first = subprocess.Popen([sys.executable, "-c", script], cwd=root, env=env)
    second = subprocess.Popen([sys.executable, "-c", script], cwd=root, env=env)

    assert first.wait(timeout=15) == 0
    assert second.wait(timeout=15) == 0


def test_schema_migration_retries_transient_wal_lock(tmp_path, monkeypatch):
    database = tmp_path / "fanout.db"
    real_connect = sqlite3.connect
    attempts = []

    class LockedJournalConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                raise sqlite3.OperationalError("database is locked")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def flaky_connect(*args, **kwargs):
        attempts.append(1)
        connection = real_connect(*args, **kwargs)
        return LockedJournalConnection(connection) if len(attempts) == 1 else connection

    monkeypatch.setattr(store.sqlite3, "connect", flaky_connect)
    monkeypatch.setattr(store, "database_path", lambda: str(database))
    store.reset_schema_cache_for_tests()

    assert store.get_model_health("m") is None
    assert len(attempts) >= 2


def test_resume_requires_explicit_unknown_retry():
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "dead", owner_pid=2_147_483_647)
    store.claim_next_result(run["id"], "dead", owner_pid=2_147_483_647)
    store.reconcile_stale_runs(now=time.time() + 301)
    assert store.resume_run(run["id"]) is None
    resumed = store.resume_run(run["id"], retry_unknown=True)
    assert resumed["status"] == "queued"
    assert store.list_results(run["id"])[0]["status"] == "pending"


def test_resume_clears_failure_class_and_provider_retry_hint():
    run = store.create_run("question", ["a"])
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    claim = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())
    assert store.record_result(
        run["id"], claim["model"], "worker", "failed", error="safe failure",
        failure_class="throttled", retry_after_ts=time.time() + 60,
    )

    assert store.resume_run(run["id"], include_failed=True)
    resumed = store.list_results(run["id"])[0]

    assert resumed["status"] == "pending"
    assert resumed["failure_class"] is None
    assert resumed["retry_after_ts"] is None


def test_failure_class_is_closed_and_retry_expiry_is_bounded(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    run = store.create_run("question", ["a"])
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid())
    claim = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid())

    result = store.record_result(
        run["id"], claim["model"], "worker", "failed", error="safe failure",
        failure_class="provider body should not become a class", retry_after_ts=99_999.0,
    )

    assert result["failure_class"] == "unknown"
    assert result["retry_after_ts"] == now[0] + store.MAX_PROVIDER_RETRY_SECONDS
    assert store.retry_after_timestamp(99_999.0, now=now[0]) == now[0] + store.MAX_PROVIDER_RETRY_SECONDS


def test_resume_requeues_unclaimed_pending_targets_without_retrying_unknown():
    run = store.create_run("question", ["claimed", "unclaimed"])
    store.claim_run(run["id"], "dead", owner_pid=2_147_483_647)
    store.claim_next_result(run["id"], "dead", owner_pid=2_147_483_647)
    store.reconcile_stale_runs(now=time.time() + 301)

    resumed = store.resume_run(run["id"])

    assert resumed["status"] == "queued"
    statuses = {result["model"]: result["status"] for result in store.list_results(run["id"])}
    assert statuses == {"claimed": "unknown", "unclaimed": "pending"}
    store.claim_run(run["id"], "next", owner_pid=os.getpid())
    assert store.claim_next_result(run["id"], "next", owner_pid=os.getpid())["model"] == "unclaimed"


def test_lease_transfer_marks_old_inflight_result_unknown(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    run = store.create_run("question", ["a"])
    store.claim_run(run["id"], "old", owner_pid=os.getpid(), lease_seconds=30)
    store.claim_next_result(run["id"], "old", owner_pid=os.getpid(), lease_seconds=30)
    now[0] += 31

    # A claim-time expiry may be discovered after the caller's preceding
    # stale-reconciliation pass.  Retiring the only in-flight child must
    # terminalize the parent instead of handing a successor an empty running
    # receipt for another lease period.
    assert store.claim_run(run["id"], "new", owner_pid=os.getpid()) is None
    result = store.list_results(run["id"])[0]
    assert result["status"] == "unknown"
    assert result["failure_class"] == "execution_uncertain"
    parent = store.get_run(run["id"])
    assert parent["status"] == "interrupted"
    assert parent["finished_ts"] == now[0]
    assert store.record_result(run["id"], "a", "old", "answered", answer="late") is None


def test_request_timeout_lease_has_completion_margin(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    run = store.create_run("question", ["a"])
    assert store.claim_run(run["id"], "worker", owner_pid=os.getpid(), lease_seconds=360)
    claim = store.claim_next_result(run["id"], "worker", owner_pid=os.getpid(), lease_seconds=360)
    now[0] += 301  # A 300-second request still has time to commit its receipt.
    assert store.record_result(run["id"], claim["model"], "worker", "answered", answer="done")


def test_process_instance_token_fences_identical_pid_thread_workers(monkeypatch):
    """Cross-host workers can share numeric PID/thread values but not identity."""
    monkeypatch.setattr(server.os, "getpid", lambda: 417)
    monkeypatch.setattr(server.threading, "get_ident", lambda: 9)
    monkeypatch.setattr(server, "_FANOUT_WORKER_INSTANCE", "host-a-instance")
    worker_a = server._fanout_worker_id()
    monkeypatch.setattr(server, "_FANOUT_WORKER_INSTANCE", "host-b-instance")
    worker_b = server._fanout_worker_id()
    assert worker_a != worker_b

    run = store.create_run("question", ["a", "b"])
    assert store.claim_run(run["id"], worker_a, owner_pid=417, lease_seconds=60)
    assert store.claim_run(run["id"], worker_b, owner_pid=417, lease_seconds=60) is None
    claimed = store.claim_next_result(run["id"], worker_a, owner_pid=417, lease_seconds=60)
    assert claimed is not None
    assert store.record_result(run["id"], claimed["model"], worker_b, "answered", answer="spoofed") is None


def test_redactor_covers_quoted_json_and_spaced_credentials():
    run = store.create_run(
        '{"api_key": "supersecret123", "token": \'secret value here\'}', ["a"],
    )

    assert "supersecret123" not in run["prompt"]
    assert "secret value here" not in run["prompt"]
