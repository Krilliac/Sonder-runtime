import memory_store as ms


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
