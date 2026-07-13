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


def test_record_outcome_row():
    c = _conn()
    ms.log_interaction(c, "abc", "t", "", "r", "code")
    ms.record_outcome_row(c, "abc", "tests_passed", 1.0)
    row = c.execute("SELECT signal, reward FROM outcomes WHERE interaction_id='abc'").fetchone()
    assert row[0] == "tests_passed"
    assert row[1] == 1.0


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
    assert ms.outcome_signal_counts(c) == {"tests_passed": 2, "failed": 1}


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
