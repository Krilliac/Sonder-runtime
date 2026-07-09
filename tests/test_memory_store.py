import embeddings as _e
import memory_store as ms


def test_delete_lesson_removes_from_table_and_fts():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "Use collections.deque for O(1) pops.", _e.to_blob([1.0, 0.0]), "i1")
    ms.add_lesson(c, "L2", "Memoize with functools.lru_cache.", _e.to_blob([0.0, 1.0]), "i2")
    ms.log_lesson_usage(c, ["L1"], "i1", "task")
    assert ms.delete_lesson(c, "L1") is True
    assert ms.get_lesson_text(c, "L1") is None
    assert [l["id"] for l in ms.all_lessons(c)] == ["L2"]
    # gone from the FTS mirror too (deque token no longer matches)
    assert "L1" not in ms.fts_search(c, "deque pops")
    assert "L1" not in ms.lesson_usage_stats(c)
    # deleting a missing id is a harmless no-op
    assert ms.delete_lesson(c, "nope") is False


def _conn():
    return ms.connect(":memory:")


def test_new_id_is_16_hex():
    i = ms.new_id()
    assert len(i) == 16
    int(i, 16)  # parses as hex


def test_log_and_get_interaction_roundtrip():
    c = _conn()
    ms.log_interaction(c, "abc", "do X", "ctx", "resp", "code")
    got = ms.get_interaction(c, "abc")
    assert got["task"] == "do X"
    assert got["response"] == "resp"
    assert got["tier"] == "code"


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
    ms.add_lesson(c, "L1", "always free the lock", b"\x00\x01", "abc")
    assert ms.get_lesson_text(c, "L1") == "always free the lock"
    lessons = ms.all_lessons(c)
    assert lessons[0]["id"] == "L1"
    assert lessons[0]["embedding"] == b"\x00\x01"


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
    for t in threads: t.start()
    for t in threads: t.join()
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
