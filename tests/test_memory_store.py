import sqlite3
import threading

import embeddings as _e
import memory_store as ms
import pytest


def test_delete_lesson_removes_from_table_and_fts():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "Use collections.deque for O(1) pops.", _e.to_blob([1.0, 0.0]), "i1")
    ms.add_lesson(c, "L2", "Memoize with functools.lru_cache.", _e.to_blob([0.0, 1.0]), "i2")
    ms.log_lesson_usage(c, ["L1"], "i1", "task")
    assert ms.delete_lesson(c, "L1") is True
    assert ms.get_lesson_text(c, "L1") is None
    assert [lesson["id"] for lesson in ms.all_lessons(c)] == ["L2"]
    # gone from the FTS mirror too (deque token no longer matches)
    assert "L1" not in ms.fts_search(c, "deque pops")
    assert "L1" not in ms.lesson_usage_stats(c)
    # deleting a missing id is a harmless no-op
    assert ms.delete_lesson(c, "nope") is False


def _conn():
    return ms.connect(":memory:")


def test_connect_migrates_lesson_usage_outcome_timestamp(tmp_path):
    path = tmp_path / "legacy-memory.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE lesson_usage ("
        "lesson_id TEXT, interaction_id TEXT, task TEXT, outcome_signal TEXT, "
        "reward REAL, ts TEXT DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(lesson_id, interaction_id))"
    )
    legacy.commit()
    legacy.close()

    conn = ms.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lesson_usage)")}
    conn.close()

    assert "outcome_ts" in columns


def test_outcome_and_distillation_migration_is_deterministic_and_one_time(tmp_path):
    path = tmp_path / "legacy-outcomes.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE interactions ("
        "id TEXT PRIMARY KEY, task TEXT, retrieved_ctx TEXT, response TEXT, "
        "tier TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE outcomes ("
        "interaction_id TEXT, signal TEXT, reward REAL, "
        "ts TEXT DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE lessons ("
        "id TEXT PRIMARY KEY, text TEXT, embedding BLOB, "
        "source_interaction TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);"
    )
    legacy.executemany(
        "INSERT INTO interactions(id, task, response, tier) VALUES(?, ?, ?, ?)",
        (
            ("stored", "task", "response", "code"),
            ("without-lesson", "task", "response", "code"),
            ("bad-only", "task", "response", "code"),
        ),
    )
    legacy.executemany(
        "INSERT INTO outcomes(interaction_id, signal, reward, ts) "
        "VALUES(?, ?, ?, ?)",
        (
            ("stored", "tests_passed", 1.0, "2020-01-01 00:00:00"),
            ("stored", "tests_passed", 0.9, "2021-01-01 00:00:00"),
            ("stored", None, 1.0, "2020-01-01 00:00:00"),
            ("stored", None, 1.0, "2021-01-01 00:00:00"),
            ("without-lesson", "compiled", 0.7, "2020-01-01 00:00:00"),
            ("bad-only", "failed", -1.0, "2020-01-01 00:00:00"),
        ),
    )
    # Shared provenance remains legal; source_interaction is intentionally not unique.
    legacy.executemany(
        "INSERT INTO lessons(id, text, source_interaction) VALUES(?, ?, ?)",
        (
            ("legacy-a", "first", "stored"),
            ("legacy-b", "second", "stored"),
        ),
    )
    legacy.commit()
    legacy.close()

    conn = ms.connect(path)
    stored_outcomes = conn.execute(
        "SELECT signal, reward, ts FROM outcomes WHERE interaction_id='stored' "
        "ORDER BY rowid"
    ).fetchall()
    assert [(row["signal"], row["reward"]) for row in stored_outcomes] == [
        ("tests_passed", 1.0),
        (None, 1.0),
        (None, 1.0),
    ]
    jobs = {
        row["interaction_id"]: row["state"]
        for row in conn.execute(
            "SELECT interaction_id, state FROM lesson_distillations"
        )
    }
    assert jobs == {
        "stored": ms.DISTILLATION_STORED,
        "without-lesson": ms.DISTILLATION_LEGACY_NO_LESSON,
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_migrations WHERE name=?",
        (ms._DISTILLATION_BACKFILL_MIGRATION,),
    ).fetchone()[0] == 1
    unique_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='uq_outcomes_interaction_signal_nonnull'"
    ).fetchone()[0]
    assert "WHERE interaction_id IS NOT NULL AND signal IS NOT NULL" in unique_sql

    conn.execute(
        "INSERT INTO interactions(id, task, response, tier) "
        "VALUES('post-migration', 'task', 'response', 'code')"
    )
    conn.execute(
        "INSERT INTO outcomes(interaction_id, signal, reward) "
        "VALUES('post-migration', 'tests_passed', 1.0)"
    )
    conn.commit()
    conn.close()

    reopened = ms.connect(path)
    assert reopened.execute(
        "SELECT 1 FROM lesson_distillations "
        "WHERE interaction_id='post-migration'"
    ).fetchone() is None
    assert reopened.execute(
        "SELECT COUNT(*) FROM memory_migrations WHERE name=?",
        (ms._DISTILLATION_BACKFILL_MIGRATION,),
    ).fetchone()[0] == 1
    reopened.close()


def test_new_id_is_16_hex():
    i = ms.new_id()
    assert len(i) == 16
    int(i, 16)  # parses as hex


def test_log_and_get_interaction_roundtrip():
    c = _conn()
    ms.log_interaction(
        c, "abc", "do X", "ctx", "resp", "code",
        tokens_in=7, tokens_out=3, token_source="ollama",
    )
    got = ms.get_interaction(c, "abc")
    assert got["task"] == "do X"
    assert got["response"] == "resp"
    assert got["tier"] == "code"
    assert got["tokens_in"] == 7
    assert got["tokens_out"] == 3
    assert got["token_source"] == "ollama"


def _log_repairable_interaction(conn, interaction_id="repairable"):
    ms.log_interaction(
        conn, interaction_id, "original task", "retrieved lesson",
        "broken response", "code", session_id="session-1",
        task_embedding=_e.to_blob([1.0, 0.0]), tokens_in=11, tokens_out=5,
        token_source="ollama", project="project-a", project_explicit=True,
        task_embedding_model="embed-v1", task_embedding_revision="digest-1",
        task_embedding_dim=2,
    )
    return ms.get_interaction(conn, interaction_id)


def test_replace_interaction_response_cas_preserves_metadata_and_embedding():
    conn = _conn()
    expected = _log_repairable_interaction(conn)

    assert ms.replace_interaction_response_cas(
        conn, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )

    stored = ms.get_interaction(conn, "repairable")
    assert stored["response"] == "fixed response"
    assert stored["tokens_in"] == 19
    assert stored["tokens_out"] == 9
    assert stored["token_source"] == "ollama+code-repair"
    for field in (
        "id", "task", "retrieved_ctx", "tier", "session_id", "project",
        "project_explicit", "ts", "task_embedding", "task_embedding_model",
        "task_embedding_revision", "task_embedding_dim",
    ):
        assert stored[field] == expected[field]


def test_replace_interaction_response_cas_compares_nulls_safely():
    conn = _conn()
    ms.log_interaction(
        conn, "nullable", "task", None, "broken response", "code",
        session_id=None, tokens_in=None, tokens_out=None, token_source=None,
        project=None, project_explicit=False,
    )
    expected = ms.get_interaction(conn, "nullable")

    assert ms.replace_interaction_response_cas(
        conn, "nullable", expected=expected, response="fixed response",
        tokens_in=7, tokens_out=3, token_source="estimated+code-repair",
    )


def test_replace_interaction_response_cas_rejects_mismatched_snapshot_id():
    conn = _conn()
    expected = _log_repairable_interaction(conn)
    expected["id"] = "different-interaction"

    assert not ms.replace_interaction_response_cas(
        conn, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )
    assert ms.get_interaction(conn, "repairable")["response"] == "broken response"


def test_session_turn_claim_is_cross_connection_and_owner_guarded(tmp_path):
    path = tmp_path / "session-claim.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    try:
        assert ms.claim_session_turn(
            first, "shared", "owner-a", now=100,
        )
        assert not ms.claim_session_turn(
            second, "shared", "owner-b", now=101,
        )
        assert not ms.release_session_turn(second, "shared", "owner-b")
        assert ms.release_session_turn(first, "shared", "owner-a")
        assert ms.claim_session_turn(
            second, "shared", "owner-b", now=103,
        )
    finally:
        first.close()
        second.close()


def test_session_turn_claim_dead_owner_can_be_recovered(tmp_path):
    path = tmp_path / "stale-session-claim.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))

    def first_probe(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_ALIVE, "old-instance"

    def second_probe(pid, expected_identity=None):
        if expected_identity == "new-instance":
            return ms.process_liveness.PROCESS_ALIVE, "new-instance"
        return ms.process_liveness.PROCESS_DEAD, None

    try:
        assert ms.claim_session_turn(
            first, "shared", "stale-owner", owner_pid=111,
            owner_identity="old-instance", now=100, owner_probe=first_probe,
        )
        assert ms.claim_session_turn(
            second, "shared", "new-owner", owner_pid=222, now=111,
            owner_identity="new-instance", owner_probe=second_probe,
        )
        row = second.execute(
            "SELECT claim_token FROM session_turn_claims WHERE session_id='shared'"
        ).fetchone()
        assert row["claim_token"] == "new-owner"
    finally:
        first.close()
        second.close()


def test_init_db_replaces_obsolete_lease_claim_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE session_turn_claims ("
        "session_id TEXT PRIMARY KEY, claim_token TEXT NOT NULL, "
        "claimed_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )

    ms.init_db(conn)

    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(session_turn_claims)"
        ).fetchall()
    }
    assert columns == {
        "session_id", "claim_token", "owner_pid", "owner_identity",
        "claimed_at",
    }


def test_session_turn_claim_unknown_owner_state_fails_closed(tmp_path):
    path = tmp_path / "unknown-owner-claim.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))

    def first_probe(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_ALIVE, "instance-a"

    def second_probe(pid, expected_identity=None):
        if expected_identity == "instance-b":
            return ms.process_liveness.PROCESS_ALIVE, "instance-b"
        return ms.process_liveness.PROCESS_UNKNOWN, None

    try:
        assert ms.claim_session_turn(
            first, "shared", "owner-a", owner_pid=111,
            owner_identity="instance-a", owner_probe=first_probe,
        )
        assert not ms.claim_session_turn(
            second, "shared", "owner-b", owner_pid=222,
            owner_identity="instance-b", owner_probe=second_probe,
        )
        row = second.execute(
            "SELECT claim_token FROM session_turn_claims WHERE session_id='shared'"
        ).fetchone()
        assert row["claim_token"] == "owner-a"
    finally:
        first.close()
        second.close()


def test_abandoned_same_process_claim_is_reclaimable(tmp_path):
    path = tmp_path / "abandoned-owner-claim.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    identity = "same-process-instance"

    def alive(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_ALIVE, expected_identity

    try:
        assert ms.claim_session_turn(
            first, "shared", "orphaned-token", owner_pid=111,
            owner_identity=identity, owner_probe=alive,
        )
        assert ms.abandon_session_turn_claim(
            "shared", "orphaned-token", 111, identity,
        )
        assert ms.claim_session_turn(
            second, "shared", "replacement-token", owner_pid=111,
            owner_identity=identity, owner_probe=alive,
        )
    finally:
        first.close()
        second.close()


def test_abandoned_marker_cannot_cross_session_on_token_reuse(tmp_path):
    path = tmp_path / "cross-session-token.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    identity = "same-process-instance"

    def alive(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_ALIVE, expected_identity

    try:
        assert ms.claim_session_turn(
            first, "session-a", "reused-token", owner_pid=111,
            owner_identity=identity, owner_probe=alive,
        )
        assert ms.abandon_session_turn_claim(
            "session-a", "reused-token", 111, identity,
        )
        assert ms.claim_session_turn(
            second, "session-b", "reused-token", owner_pid=111,
            owner_identity=identity, owner_probe=alive,
        )
        assert not ms.claim_session_turn(
            first, "session-b", "third-token", owner_pid=111,
            owner_identity=identity, owner_probe=alive,
        )
    finally:
        ms.release_session_turn(first, "session-a", "reused-token")
        ms.release_session_turn(second, "session-b", "reused-token")
        first.close()
        second.close()


@pytest.mark.parametrize(
    ("column", "changed_value"),
    (
        ("response", "concurrent response"),
        ("tokens_in", 101),
        ("tokens_out", 102),
        ("token_source", "estimated"),
        ("task", "concurrent task"),
        ("retrieved_ctx", "concurrent context"),
        ("tier", "general"),
        ("session_id", "session-2"),
        ("project", "project-b"),
        ("project_explicit", 0),
    ),
)
def test_replace_interaction_response_cas_rejects_guarded_field_conflicts(
    column, changed_value,
):
    conn = _conn()
    expected = _log_repairable_interaction(conn)
    conn.execute(
        "UPDATE interactions SET %s=? WHERE id='repairable'" % column,
        (changed_value,),
    )
    conn.commit()
    concurrent = ms.get_interaction(conn, "repairable")

    assert not ms.replace_interaction_response_cas(
        conn, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )
    assert ms.get_interaction(conn, "repairable") == concurrent


@pytest.mark.parametrize("blocker", ("outcome", "lesson", "credited_usage"))
def test_replace_interaction_response_cas_rejects_learning_state(blocker):
    conn = _conn()
    expected = _log_repairable_interaction(conn)
    if blocker == "outcome":
        ms.record_outcome_row(conn, "repairable", "tests_passed", 1.0)
    elif blocker == "lesson":
        ms.add_lesson(conn, "derived", "derived lesson", None, "repairable")
    else:
        ms.add_lesson(conn, "retrieved", "retrieved lesson", None, "seed")
        ms.log_lesson_usage(conn, ["retrieved"], "repairable", "original task")
        ms.record_lesson_usage_outcome(
            conn, "repairable", "tests_passed", 1.0,
        )

    assert not ms.replace_interaction_response_cas(
        conn, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )
    assert ms.get_interaction(conn, "repairable") == expected


def test_replace_interaction_response_cas_allows_uncredited_lesson_usage():
    conn = _conn()
    expected = _log_repairable_interaction(conn)
    ms.add_lesson(conn, "retrieved", "retrieved lesson", None, "seed")
    ms.log_lesson_usage(conn, ["retrieved"], "repairable", "original task")

    assert ms.replace_interaction_response_cas(
        conn, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )


def test_replace_interaction_response_cas_allows_concurrent_embedding_refresh(
    tmp_path,
):
    path = tmp_path / "repair-memory.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    expected = _log_repairable_interaction(first)

    refreshed = _e.to_blob([0.0, 1.0])
    assert ms.refresh_interaction_task_embedding(
        second, "repairable", refreshed, "embed-v2",
        revision="digest-2", dimension=2,
    )
    assert ms.replace_interaction_response_cas(
        first, "repairable", expected=expected, response="fixed response",
        tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
    )

    stored = ms.get_interaction(first, "repairable")
    assert stored["response"] == "fixed response"
    assert stored["task_embedding"] == refreshed
    assert stored["task_embedding_model"] == "embed-v2"
    assert stored["task_embedding_revision"] == "digest-2"
    assert stored["task_embedding_dim"] == 2
    first.close()
    second.close()


def test_replace_interaction_response_cas_rolls_back_on_error():
    conn = _conn()
    expected = _log_repairable_interaction(conn)
    conn.execute(
        "UPDATE interactions SET task='uncommitted task' WHERE id='repairable'"
    )
    conn.execute("DROP TABLE outcomes")

    with pytest.raises(sqlite3.OperationalError, match="no such table: outcomes"):
        ms.replace_interaction_response_cas(
            conn, "repairable", expected=expected, response="fixed response",
            tokens_in=19, tokens_out=9, token_source="ollama+code-repair",
        )

    assert ms.get_interaction(conn, "repairable")["task"] == "original task"
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_interaction_token_totals_mix_exact_and_estimated_rows():
    c = _conn()
    ms.log_interaction(c, "exact", "task", "ctx", "response", "code",
                       tokens_in=10, tokens_out=5, token_source="ollama")
    ms.log_interaction(c, "legacy", "12345", "123", "12345678", "fast")

    totals = ms.interaction_token_totals(c)
    assert totals["interactions"] == 2
    assert totals["exact_rows"] == 1
    assert totals["estimated_rows"] == 1
    assert totals["tokens_in"] == 12  # 10 exact + ceil(8 chars / 4)
    assert totals["tokens_out"] == 7  # 5 exact + ceil(8 chars / 4)
    assert totals["tokens_total"] == 19

    by_tier = {row["tier"]: row for row in ms.interaction_token_totals_by_tier(c)}
    assert by_tier["code"]["tokens_total"] == 15
    assert by_tier["fast"]["tokens_total"] == 4


def test_get_missing_interaction_returns_none():
    assert ms.get_interaction(_conn(), "nope") is None


def test_delete_interaction_removes_distillation_trace():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    ms.delete_interaction(conn, "job")

    assert conn.execute(
        "SELECT 1 FROM lesson_distillations WHERE interaction_id='job'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM outcomes WHERE interaction_id='job'"
    ).fetchone() is None


def test_record_outcome_row():
    c = _conn()
    ms.log_interaction(c, "abc", "t", "", "r", "code")
    assert ms.record_outcome_row(c, "abc", "tests_passed", 1.0) is True
    assert ms.record_outcome_row(c, "abc", "tests_passed", 0.9) is False
    assert ms.record_outcome_row(c, "abc", "compiled", 0.7) is True
    row = c.execute(
        "SELECT signal, reward FROM outcomes WHERE interaction_id='abc' "
        "AND signal='tests_passed'"
    ).fetchone()
    assert row[0] == "tests_passed"
    assert row[1] == 1.0


def _owner_probe(pid, expected_identity=None):
    return ms.process_liveness.PROCESS_ALIVE, expected_identity or "owner-%s" % pid


def _claim_good_outcome(
    conn, interaction_id="job", token="claim-1", owner_pid=101,
    owner_identity="owner-101", probe=_owner_probe,
):
    return ms.record_outcome_and_claim_lesson_distillation(
        conn, interaction_id, "tests_passed", 1.0,
        claim_token=token, owner_pid=owner_pid,
        owner_identity=owner_identity, owner_probe=probe, now=100,
    )


def test_atomic_outcome_duplicate_preserves_usage_time_but_reclaims_retryable():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    ms.add_lesson(conn, "seed", "seed lesson", None, "seed-source")
    ms.log_lesson_usage(conn, ["seed"], "job", "task")

    first = _claim_good_outcome(conn)
    assert first == {
        "outcome_inserted": True,
        "usage_rows_updated": 1,
        "distillation_state": ms.DISTILLATION_CLAIMED,
        "claimed": True,
        "claim_token": "claim-1",
        "attempts": 1,
    }
    assert ms.mark_lesson_distillation_retryable(
        conn, "job", "claim-1", "temporary model failure",
    )
    conn.execute(
        "UPDATE lesson_usage SET outcome_ts='2000-01-01 00:00:00' "
        "WHERE interaction_id='job'"
    )
    conn.commit()

    duplicate = _claim_good_outcome(conn, token="claim-2")
    assert duplicate["outcome_inserted"] is False
    assert duplicate["usage_rows_updated"] == 0
    assert duplicate["claimed"] is True
    assert duplicate["claim_token"] == "claim-2"
    assert duplicate["attempts"] == 2
    assert conn.execute(
        "SELECT outcome_ts FROM lesson_usage WHERE interaction_id='job'"
    ).fetchone()[0] == "2000-01-01 00:00:00"

    different = ms.record_outcome_and_claim_lesson_distillation(
        conn, "job", "accepted", 0.8, claim_token="claim-2",
        owner_pid=101, owner_identity="owner-101", owner_probe=_owner_probe,
        now=101,
    )
    assert different["outcome_inserted"] is True
    assert different["usage_rows_updated"] == 1
    assert different["claimed"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE interaction_id='job'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT outcome_ts FROM lesson_usage WHERE interaction_id='job'"
    ).fetchone()[0] != "2000-01-01 00:00:00"


def test_atomic_outcome_unknown_owner_fails_closed_and_dead_owner_recovers():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")

    assert _claim_good_outcome(conn)["claimed"] is True

    def unknown_probe(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_UNKNOWN, None

    blocked = _claim_good_outcome(
        conn, token="blocked", owner_pid=202, owner_identity="owner-202",
        probe=unknown_probe,
    )
    assert blocked["claimed"] is False
    assert blocked["distillation_state"] == ms.DISTILLATION_CLAIMED
    assert conn.execute(
        "SELECT claim_token FROM lesson_distillations WHERE interaction_id='job'"
    ).fetchone()[0] == "claim-1"

    def recovery_probe(pid, expected_identity=None):
        if pid == 202 and expected_identity == "owner-202":
            return ms.process_liveness.PROCESS_ALIVE, "owner-202"
        if pid == 101 and expected_identity == "owner-101":
            return ms.process_liveness.PROCESS_DEAD, None
        return ms.process_liveness.PROCESS_UNKNOWN, None

    recovered = _claim_good_outcome(
        conn, token="recovered", owner_pid=202, owner_identity="owner-202",
        probe=recovery_probe,
    )
    assert recovered["outcome_inserted"] is False
    assert recovered["claimed"] is True
    assert recovered["claim_token"] == "recovered"
    assert recovered["attempts"] == 2


def test_initial_unknown_owner_leaves_retryable_job_for_duplicate_reacquire():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")

    def unknown_probe(pid, expected_identity=None):
        return ms.process_liveness.PROCESS_UNKNOWN, None

    first = _claim_good_outcome(conn, probe=unknown_probe)
    assert first["outcome_inserted"] is True
    assert first["claimed"] is False
    assert first["distillation_state"] == ms.DISTILLATION_RETRYABLE
    assert first["attempts"] == 0

    recovered = _claim_good_outcome(conn, token="claim-2")
    assert recovered["outcome_inserted"] is False
    assert recovered["claimed"] is True
    assert recovered["distillation_state"] == ms.DISTILLATION_CLAIMED
    assert recovered["attempts"] == 1

    idempotent = _claim_good_outcome(conn, token="claim-2")
    assert idempotent["claimed"] is True
    assert idempotent["attempts"] == 1


def test_abandoned_same_process_distillation_claim_is_reclaimable():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True
    assert ms.abandon_lesson_distillation_claim(
        "job", "claim-1", 101, "owner-101",
    )

    reclaimed = _claim_good_outcome(conn, token="claim-2")

    assert reclaimed["claimed"] is True
    assert reclaimed["claim_token"] == "claim-2"
    assert reclaimed["attempts"] == 2


def test_concurrent_same_outcome_has_one_row_and_one_claim_owner(tmp_path):
    path = tmp_path / "concurrent-outcome.db"
    seed = ms.connect(path)
    ms.log_interaction(seed, "job", "task", "", "response", "code")
    seed.close()

    worker_count = 6
    barrier = threading.Barrier(worker_count)
    results = []
    errors = []
    result_lock = threading.Lock()

    def worker(index):
        conn = ms.connect(path)
        try:
            barrier.wait(timeout=5)
            result = ms.record_outcome_and_claim_lesson_distillation(
                conn, "job", "tests_passed", 1.0,
                claim_token="claim-%d" % index,
                owner_pid=1000 + index,
                owner_identity="owner-%d" % index,
                owner_probe=_owner_probe,
                now=100 + index,
            )
            with result_lock:
                results.append(result)
        except Exception as exc:
            with result_lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == worker_count
    assert sum(result["outcome_inserted"] for result in results) == 1
    assert sum(result["claimed"] for result in results) == 1

    conn = ms.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE interaction_id='job' "
        "AND signal='tests_passed'"
    ).fetchone()[0] == 1
    row = conn.execute(
        "SELECT state, attempts FROM lesson_distillations "
        "WHERE interaction_id='job'"
    ).fetchone()
    assert row["state"] == ms.DISTILLATION_CLAIMED
    assert row["attempts"] == 1
    conn.close()


def test_non_good_evidence_cancels_live_claim_and_blocks_finalization():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    failed = ms.record_outcome_and_claim_lesson_distillation(
        conn, "job", "failed", -1.0, claim_token="unused",
        owner_pid=101, owner_identity="owner-101", owner_probe=_owner_probe,
    )
    assert failed["outcome_inserted"] is True
    assert failed["claimed"] is False
    assert failed["distillation_state"] == ms.DISTILLATION_CANCELLED
    row = conn.execute(
        "SELECT state, claim_token, owner_pid, completed_ts "
        "FROM lesson_distillations WHERE interaction_id='job'"
    ).fetchone()
    assert row["state"] == ms.DISTILLATION_CANCELLED
    assert row["claim_token"] is None
    assert row["owner_pid"] is None
    assert row["completed_ts"] is not None

    called = []
    finalized = ms.finalize_lesson_distillation(
        conn, "job", "claim-1", lambda tx: called.append(True),
    )
    assert finalized["finalized"] is False
    assert finalized["distillation_state"] == ms.DISTILLATION_CANCELLED
    assert called == []


def test_finalization_rechecks_legacy_contradiction_before_callback():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True
    # Simulate an older writer that records evidence without touching the ledger.
    assert ms.record_outcome_row(conn, "job", "rejected", -0.5)

    called = []
    result = ms.finalize_lesson_distillation(
        conn,
        "job",
        "claim-1",
        lambda tx: called.append(True),
    )
    assert result == {
        "finalized": False,
        "distillation_state": ms.DISTILLATION_CANCELLED,
        "lesson_id": None,
        "result": None,
    }
    assert called == []


def test_finalization_callback_atomically_inserts_lesson_and_fts():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    def store(tx):
        lesson_id = ms.insert_lesson_in_transaction(
            tx, "lesson-1", "Use a bounded transaction callback.", None, "job",
        )
        return {
            "terminal_state": ms.DISTILLATION_STORED,
            "lesson_id": lesson_id,
            "result": {"dedupe": "unique"},
        }

    result = ms.finalize_lesson_distillation(conn, "job", "claim-1", store)
    assert result == {
        "finalized": True,
        "distillation_state": ms.DISTILLATION_STORED,
        "lesson_id": "lesson-1",
        "result": {"dedupe": "unique"},
    }
    assert conn.execute(
        "SELECT state, claim_token FROM lesson_distillations "
        "WHERE interaction_id='job'"
    ).fetchone()["state"] == ms.DISTILLATION_STORED
    assert conn.execute(
        "SELECT text FROM lessons WHERE id='lesson-1'"
    ).fetchone()[0] == "Use a bounded transaction callback."
    assert conn.execute(
        "SELECT text FROM lessons_fts WHERE lesson_id='lesson-1'"
    ).fetchone()[0] == "Use a bounded transaction callback."


def test_finalization_callback_selects_no_lesson_and_returns_metadata():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    result = ms.finalize_lesson_distillation(
        conn,
        "job",
        "claim-1",
        lambda tx: {
            "terminal_state": ms.DISTILLATION_NO_LESSON,
            "result": {"dedupe": "exact"},
        },
    )
    assert result == {
        "finalized": True,
        "distillation_state": ms.DISTILLATION_NO_LESSON,
        "lesson_id": None,
        "result": {"dedupe": "exact"},
    }


def test_finalization_callback_failure_rolls_back_lesson_fts_and_ledger():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    def fail_after_insert(tx):
        ms.insert_lesson_in_transaction(
            tx, "rolled-back", "must disappear", None, "job",
        )
        raise RuntimeError("distiller crashed")

    with pytest.raises(RuntimeError, match="distiller crashed"):
        ms.finalize_lesson_distillation(
            conn, "job", "claim-1", fail_after_insert,
        )

    assert conn.execute(
        "SELECT 1 FROM lessons WHERE id='rolled-back'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM lessons_fts WHERE lesson_id='rolled-back'"
    ).fetchone() is None
    row = conn.execute(
        "SELECT state, claim_token FROM lesson_distillations "
        "WHERE interaction_id='job'"
    ).fetchone()
    assert row["state"] == ms.DISTILLATION_CLAIMED
    assert row["claim_token"] == "claim-1"
    assert ms.mark_lesson_distillation_retryable(
        conn, "job", "claim-1", "distiller crashed",
    )


def test_explicit_cancel_and_transition_tokens_are_guarded():
    conn = _conn()
    ms.log_interaction(conn, "job", "task", "", "response", "code")
    assert _claim_good_outcome(conn)["claimed"] is True

    assert not ms.mark_lesson_distillation_retryable(
        conn, "job", "wrong", "wrong owner",
    )
    assert not ms.cancel_lesson_distillation(
        conn, "job", "wrong owner", claim_token="wrong",
    )
    assert ms.cancel_lesson_distillation(
        conn, "job", "operator cancelled", claim_token="claim-1",
    )
    assert not ms.cancel_lesson_distillation(conn, "job", "already terminal")


def test_add_lesson_and_read_back():
    c = _conn()
    vector = _e.to_blob([1.0, 0.0])
    ms.add_lesson(c, "L1", "always free the lock", vector, "abc")
    assert ms.get_lesson_text(c, "L1") == "always free the lock"
    lessons = ms.all_lessons(c)
    assert lessons[0]["id"] == "L1"
    assert lessons[0]["embedding"] == vector


def test_missing_lesson_embeddings_can_be_backfilled_without_overwrite():
    conn = ms.connect(":memory:")
    new_vector = _e.to_blob([1.0, 0.0])
    existing_vector = _e.to_blob([0.0, 1.0])
    ms.add_lesson(conn, "missing", "needs a vector", None, "seed")
    ms.add_lesson(
        conn, "present", "already vectorized", existing_vector, "seed",
    )

    rows = ms.lessons_without_embeddings(conn, limit=10)

    assert [row["id"] for row in rows] == ["missing"]
    assert ms.set_lesson_embedding(conn, "missing", new_vector) is True
    assert ms.set_lesson_embedding(conn, "present", new_vector) is False
    stored = {row["id"]: row["embedding"] for row in ms.all_lessons(conn)}
    assert stored == {"missing": new_vector, "present": existing_vector}


def test_embedding_refresh_tracks_model_revision_and_dimension():
    conn = ms.connect(":memory:")
    ms.add_lesson(
        conn, "legacy", "legacy vector", _e.to_blob([1.0, 0.0]), "seed",
    )
    ms.add_lesson(
        conn, "current", "current vector", _e.to_blob([1.0, 0.0, 0.0]), "seed",
        embedding_model="embed-v2", embedding_revision="digest-2",
        embedding_dim=3,
    )

    stale = ms.lessons_needing_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=3,
    )

    assert [row["id"] for row in stale] == ["legacy"]
    assert ms.count_lessons_needing_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=3,
    ) == 1
    assert ms.refresh_lesson_embedding(
        conn, "legacy", _e.to_blob([1.0, 0.0, 0.0]), "embed-v2",
        revision="digest-2", dimension=3,
    )
    assert ms.count_lessons_needing_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=3,
    ) == 0
    stats = ms.embedding_provenance_stats(
        conn, "embed-v2", revision="digest-2", dimension=3,
    )
    assert stats["embedded"] == 2
    assert stats["legacy_model"] == 0
    assert stats["dimensions"] == {"3": 2}


def test_unversioned_revision_expectation_rejects_hashed_vectors():
    conn = ms.connect(":memory:")
    ms.add_lesson(
        conn, "stale", "stale revision", _e.to_blob([1.0, 0.0]), "seed",
        embedding_model="embed-v2", embedding_revision="stale-hash",
        embedding_dim=2,
    )

    selected = ms.lessons_needing_embedding_refresh(
        conn, "embed-v2", revision="", dimension=2,
    )

    assert [row["id"] for row in selected] == ["stale"]


def test_embedding_refresh_compare_and_swap_preserves_concurrent_update(tmp_path):
    path = tmp_path / "memory.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    ms.add_lesson(first, "lesson", "text", _e.to_blob([1.0, 0.0]), "seed")
    selected = ms.lessons_needing_embedding_refresh(
        first, "embed-v2", revision="digest-2", dimension=3,
    )[0]

    assert ms.refresh_lesson_embedding(
        second, "lesson", _e.to_blob([1.0, 0.0, 0.0]), "embed-v2",
        revision="newer", dimension=3,
    )
    assert not ms.refresh_lesson_embedding(
        first, "lesson", _e.to_blob([0.0, 1.0, 0.0]), "embed-v2",
        revision="digest-2", dimension=3, expected=selected,
    )

    row = first.execute(
        "SELECT embedding, embedding_revision FROM lessons WHERE id='lesson'"
    ).fetchone()
    assert _e.from_blob(row["embedding"]) == [1.0, 0.0, 0.0]
    assert row["embedding_revision"] == "newer"
    first.close()
    second.close()


def test_embedding_refresh_cas_also_binds_source_text():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "old text", None, "seed")
    selected = ms.lessons_needing_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=2,
    )[0]
    conn.execute("UPDATE lessons SET text='new text' WHERE id='lesson'")
    conn.commit()

    assert not ms.refresh_lesson_embedding(
        conn, "lesson", _e.to_blob([1.0, 0.0]), "embed-v2",
        revision="digest-2", dimension=2, expected=selected,
    )


def test_embedding_write_apis_require_strict_float32_vectors_and_dimensions():
    conn = ms.connect(":memory:")
    vector = _e.to_blob([1.0, 0.0])
    for invalid in (b"", b"xx", _e.to_blob([0.0, 0.0])):
        with pytest.raises(ValueError):
            ms.add_lesson(conn, ms.new_id(), "text", invalid, "seed")
        with pytest.raises(ValueError):
            ms.log_interaction(
                conn, ms.new_id(), "task", "", "answer", "code",
                task_embedding=invalid,
            )
    for invalid_dimension in (True, 2.5, "2"):
        with pytest.raises(ValueError):
            ms.add_lesson(
                conn, ms.new_id(), "text", vector, "seed",
                embedding_dim=invalid_dimension,
            )
        with pytest.raises(ValueError):
            ms.log_interaction(
                conn, ms.new_id(), "task", "", "answer", "code",
                task_embedding=vector, task_embedding_dim=invalid_dimension,
            )


def test_interaction_task_embedding_maintenance_is_bounded_and_integrity_aware():
    conn = ms.connect(":memory:")
    current = _e.to_blob([1.0, 0.0])
    for interaction_id, blob, model, revision in (
        ("current", current, "embed-v2", "digest-2"),
        ("missing", None, None, None),
        ("legacy", _e.to_blob([0.0, 1.0]), None, None),
        ("wrong-model", current, "embed-v1", "digest-2"),
        ("malformed", current, "embed-v2", "digest-2"),
        ("zero", current, "embed-v2", "digest-2"),
    ):
        ms.log_interaction(
            conn, interaction_id, "task %s" % interaction_id, "", "answer",
            "code", task_embedding=blob, task_embedding_model=model,
            task_embedding_revision=revision,
            task_embedding_dim=2 if blob is not None else None,
        )
    conn.execute(
        "UPDATE interactions SET task_embedding=? WHERE id='malformed'",
        (b"\x00" * 6,),
    )
    conn.execute(
        "UPDATE interactions SET task_embedding=? WHERE id='zero'",
        (_e.to_blob([0.0, 0.0]),),
    )
    conn.commit()

    selected = ms.interactions_needing_task_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=2, limit=3,
    )
    count = ms.count_interactions_needing_task_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=2,
    )
    stats = ms.interaction_task_embedding_provenance_stats(
        conn, "embed-v2", revision="digest-2", dimension=2,
    )

    assert [row["id"] for row in selected] == [
        "missing", "legacy", "wrong-model",
    ]
    assert count == 5
    assert stats["interactions"] == 6
    assert stats["compatible"] == 1
    assert stats["refresh_required"] == 5
    assert stats["missing"] == 1
    assert stats["legacy_model"] == 1
    assert stats["model_mismatch"] == 1
    assert stats["dimension_invalid"] == 1
    assert stats["vector_invalid"] == 1
    assert stats["dimensions"] == {"2": 4}
    conn.close()


def test_interaction_task_embedding_refresh_compare_and_swap(tmp_path):
    path = tmp_path / "interaction-memory.db"
    first = ms.connect(str(path))
    second = ms.connect(str(path))
    ms.log_interaction(
        first, "interaction", "task", "", "answer", "code",
        task_embedding=_e.to_blob([1.0, 0.0]),
    )
    selected = ms.interactions_needing_task_embedding_refresh(
        first, "embed-v2", revision="digest-2", dimension=2,
    )[0]

    assert ms.refresh_interaction_task_embedding(
        second, "interaction", _e.to_blob([0.0, 1.0]), "embed-v2",
        revision="newer", dimension=2,
    )
    assert not ms.refresh_interaction_task_embedding(
        first, "interaction", _e.to_blob([0.5, 0.5]), "embed-v2",
        revision="digest-2", dimension=2, expected=selected,
    )

    row = first.execute(
        "SELECT task_embedding, task_embedding_revision FROM interactions "
        "WHERE id='interaction'"
    ).fetchone()
    assert _e.from_blob(row["task_embedding"]) == [0.0, 1.0]
    assert row["task_embedding_revision"] == "newer"
    first.close()
    second.close()


def test_interaction_embedding_refresh_cas_also_binds_task_text():
    conn = ms.connect(":memory:")
    ms.log_interaction(conn, "interaction", "old task", "", "answer", "code")
    selected = ms.interactions_needing_task_embedding_refresh(
        conn, "embed-v2", revision="digest-2", dimension=2,
    )[0]
    conn.execute("UPDATE interactions SET task='new task' WHERE id='interaction'")
    conn.commit()

    assert not ms.refresh_interaction_task_embedding(
        conn, "interaction", _e.to_blob([1.0, 0.0]), "embed-v2",
        revision="digest-2", dimension=2, expected=selected,
    )


def test_repeated_init_does_not_rescan_embedding_tables():
    conn = ms.connect(":memory:")
    statements = []
    conn.set_trace_callback(statements.append)

    ms.init_db(conn)

    normalized = [statement.upper() for statement in statements]
    assert not any(
        "UPDATE INTERACTIONS SET TASK_EMBEDDING_DIM" in statement
        for statement in normalized
    )
    assert not any(
        "UPDATE LESSONS SET EMBEDDING_DIM" in statement
        for statement in normalized
    )


def test_concurrent_first_connect_serializes_legacy_schema_migration(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.execute(
        "CREATE TABLE interactions(id TEXT PRIMARY KEY, task TEXT, "
        "retrieved_ctx TEXT, response TEXT, tier TEXT, "
        "ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.commit()
    legacy.close()
    barrier = threading.Barrier(4)
    errors = []

    def migrate():
        try:
            barrier.wait()
            conn = ms.connect(str(path))
            conn.close()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    conn = ms.connect(str(path))
    try:
        columns = ms._column_names(conn, "interactions")
        assert {"session_id", "task_embedding", "project_explicit"} <= columns
    finally:
        conn.close()


def test_fts_search_matches_tokens():
    c = _conn()
    ms.add_lesson(c, "L1", "use RRF fusion for hybrid retrieval", None, "a")
    ms.add_lesson(c, "L2", "close the sqlite connection", None, "b")
    hits = ms.fts_search(c, "hybrid retrieval fusion")
    assert "L1" in hits
    assert hits[0] == "L1"


def test_fts_search_empty_query_returns_empty():
    c = _conn()
    ms.add_lesson(c, "L1", "anything", None, "a")
    assert ms.fts_search(c, "a to") == []  # only short/stopword tokens -> no query


def test_fts_search_ranks_more_relevant_first():
    c = _conn()
    ms.add_lesson(c, "L1", "use RRF fusion for hybrid retrieval ranking", None, "a")
    ms.add_lesson(c, "L2", "a hybrid approach", None, "b")
    hits = ms.fts_search(c, "hybrid retrieval fusion")
    assert "L1" in hits and "L2" in hits
    assert hits.index("L1") < hits.index("L2")  # L1 matches more query terms


def test_lesson_exists_for_interaction():
    c = _conn()
    assert ms.lesson_exists_for_interaction(c, "iX") is False
    ms.add_lesson(c, "L1", "text", None, "iX")
    assert ms.lesson_exists_for_interaction(c, "iX") is True


def test_connect_uses_wal_on_file_db(tmp_path):
    p = str(tmp_path / "wal.db")
    c = ms.connect(p)
    mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_writes_from_threads(tmp_path):
    import threading
    p = str(tmp_path / "conc.db")
    ms.connect(p)  # initialize schema/file
    errors = []
    def worker(n):
        try:
            conn = ms.connect(p)  # each thread its OWN connection
            for i in range(5):
                ms.log_interaction(conn, f"t{n}-{i}", "task", "", "resp", "code")
            conn.close()
        except Exception as e:  # noqa
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    conn = ms.connect(p)
    count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    assert count == 40  # 8 threads * 5 each


def test_count_interactions_empty():
    assert ms.count_interactions(_conn()) == 0


def test_count_interactions_counts_rows():
    c = _conn()
    ms.log_interaction(c, "a", "t", "", "r", "code")
    ms.log_interaction(c, "b", "t", "", "r", "code")
    assert ms.count_interactions(c) == 2


def test_outcome_signal_counts_empty():
    assert ms.outcome_signal_counts(_conn()) == {}


def test_outcome_signal_counts_groups_by_signal():
    c = _conn()
    ms.log_interaction(c, "a", "t", "", "r", "code")
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    ms.record_outcome_row(c, "a", "failed", -1.0)
    assert ms.outcome_signal_counts(c) == {"tests_passed": 1, "failed": 1}


def test_recent_lessons_empty():
    assert ms.recent_lessons(_conn()) == []


def test_recent_lessons_returns_most_recent_first():
    c = _conn()
    ms.add_lesson(c, "L1", "first lesson", None, "a")
    ms.add_lesson(c, "L2", "second lesson", None, "b")
    ms.add_lesson(c, "L3", "third lesson", None, "c")
    lessons = ms.recent_lessons(c, limit=2)
    assert len(lessons) == 2
    assert lessons[0]["id"] == "L3"
    assert lessons[1]["id"] == "L2"
    assert set(lessons[0].keys()) >= {"id", "text", "ts"}


def test_interactions_with_good_outcome_filters_by_signal():
    c = _conn()
    ms.log_interaction(c, "a", "task A", "", "resp A", "code")
    ms.log_interaction(c, "b", "task B", "", "resp B", "code")
    ms.log_interaction(c, "c", "task C", "", "resp C", "code")
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    ms.record_outcome_row(c, "b", "failed", -1.0)
    ms.record_outcome_row(c, "c", "compiled", 0.7)
    good = ms.interactions_with_good_outcome(c, {"tests_passed", "compiled"})
    ids = {g["id"] for g in good}
    assert ids == {"a", "c"}
    assert len(good) == 2
    tasks = {g["task"] for g in good}
    assert tasks == {"task A", "task C"}


def test_interactions_with_good_outcome_empty_signals_returns_empty():
    c = _conn()
    ms.log_interaction(c, "a", "task A", "", "resp A", "code")
    ms.record_outcome_row(c, "a", "tests_passed", 1.0)
    assert ms.interactions_with_good_outcome(c, set()) == []


def test_lesson_usage_stats_records_outcomes():
    c = _conn()
    ms.add_lesson(c, "L1", "lesson", None, "i0")
    ms.log_lesson_usage(c, ["L1"], "i1", "task")
    stats = ms.lesson_usage_stats(c)["L1"]
    assert stats["uses"] == 1
    assert stats["wins"] == 0
    ms.record_lesson_usage_outcome(c, "i1", "tests_passed", 1.0)
    stats = ms.lesson_usage_stats(c)["L1"]
    assert stats["wins"] == 1
    assert stats["avg_reward"] == 1.0


def test_preferences_upsert_and_disable():
    c = _conn()
    ms.upsert_preference(c, "p1", "global", "concise", "User prefers concise answers.")
    ms.upsert_preference(c, "p2", "global", "concise", "User prefers concise answers.")

    prefs = ms.preferences_for_scope(c)
    assert len(prefs) == 1
    assert prefs[0]["evidence_count"] == 2
    assert prefs[0]["enabled"] == 1

    assert ms.set_preference_enabled(c, "concise", False) == 1
    assert ms.preferences_for_scope(c) == []
    assert ms.preferences_for_scope(c, include_disabled=True)[0]["enabled"] == 0
