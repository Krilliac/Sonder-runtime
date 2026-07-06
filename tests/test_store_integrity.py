import embeddings
import memory_store as ms
import store_integrity as si


def _conn():
    return ms.connect(":memory:")


def _fake_decode(vec_len=4):
    """A deterministic, dependency-free stand-in for embeddings.from_blob.

    Real from_blob is pure stdlib (array.frombytes) with no model/GPU/network
    involved, but check_bad_embeddings takes decode_fn as an injected seam --
    this exercises that seam explicitly, per the "stub any injected dependency"
    rule, and proves the checker doesn't hardcode embeddings.from_blob.
    """
    def decode(blob):
        if len(blob) != vec_len * 4:
            raise ValueError("bad length: expected %d bytes" % (vec_len * 4))
        return list(range(vec_len))

    return decode


# --- healthy store -----------------------------------------------------------

def test_healthy_store_reports_ok_with_default_decoder():
    c = _conn()
    ms.add_lesson(c, "L1", "always free the lock before returning",
                  embeddings.to_blob([1.0, 0.0, 0.5]), "i1")
    ms.add_lesson(c, "L2", "memoize with functools.lru_cache",
                  embeddings.to_blob([0.0, 1.0, 0.2]), "i2")
    # a lesson with no embedding at all is legitimate (embedding is nullable)
    ms.add_lesson(c, "L3", "prefer pathlib over os.path for new code", None, "i3")

    ok, issues = si.check_store(c)

    assert ok is True
    assert issues == []


def test_healthy_store_report_text():
    ok, issues = si.check_store(_conn())
    assert ok is True
    assert si.format_report(issues) == "Lesson store OK -- no integrity issues found."


# --- orphan_fts ---------------------------------------------------------------

def test_orphan_fts_detected_when_lessons_row_deleted_but_fts_row_left():
    c = _conn()
    ms.add_lesson(c, "L1", "some lesson text", embeddings.to_blob([1.0]), "i1")
    # simulate a partial/crashed delete: remove from lessons but NOT lessons_fts
    # (memory_store.delete_lesson always does both -- this bypasses it on purpose)
    c.execute("DELETE FROM lessons WHERE id=?", ("L1",))
    c.commit()

    ok, issues = si.check_store(c)

    assert ok is False
    codes = [(i.code, i.lesson_id) for i in issues]
    assert ("orphan_fts", "L1") in codes


def test_orphan_fts_absent_on_clean_delete():
    c = _conn()
    ms.add_lesson(c, "L1", "some lesson text", embeddings.to_blob([1.0]), "i1")
    ms.delete_lesson(c, "L1")  # the real, correct deletion path

    ok, issues = si.check_store(c)

    assert ok is True
    assert issues == []


# --- missing_fts ---------------------------------------------------------------

def test_missing_fts_detected_when_lessons_row_inserted_without_mirror():
    c = _conn()
    # bypass add_lesson entirely -- insert straight into lessons only
    c.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction) VALUES(?, ?, ?, ?)",
        ("L1", "orphaned on the other side", embeddings.to_blob([1.0]), "i1"),
    )
    c.commit()

    ok, issues = si.check_store(c)

    assert ok is False
    codes = [(i.code, i.lesson_id) for i in issues]
    assert ("missing_fts", "L1") in codes


# --- empty_text -----------------------------------------------------------------

def test_empty_and_whitespace_and_null_text_all_detected():
    c = _conn()
    ms.add_lesson(c, "L1", "", embeddings.to_blob([1.0]), "i1")
    ms.add_lesson(c, "L2", "   \n\t  ", embeddings.to_blob([1.0]), "i2")
    ms.add_lesson(c, "L3", None, embeddings.to_blob([1.0]), "i3")
    ms.add_lesson(c, "L4", "this one is fine", embeddings.to_blob([1.0]), "i4")

    ok, issues = si.check_store(c)

    assert ok is False
    empty_ids = {i.lesson_id for i in issues if i.code == "empty_text"}
    assert empty_ids == {"L1", "L2", "L3"}


# --- bad_embedding --------------------------------------------------------------

def test_malformed_embedding_bytes_detected_with_real_decoder():
    c = _conn()
    # 3 bytes is not a multiple of 4 -- array.frombytes raises ValueError
    ms.add_lesson(c, "L1", "a lesson with a truncated embedding", b"\x01\x02\x03", "i1")

    ok, issues = si.check_store(c)  # default decode_fn = embeddings.from_blob

    assert ok is False
    bad = [i for i in issues if i.code == "bad_embedding" and i.lesson_id == "L1"]
    assert len(bad) == 1
    assert "failed to decode" in bad[0].detail


def test_empty_embedding_blob_decodes_to_empty_vector_and_is_flagged():
    c = _conn()
    ms.add_lesson(c, "L1", "a lesson with an empty embedding blob", b"", "i1")

    ok, issues = si.check_store(c)

    assert ok is False
    bad = [i for i in issues if i.code == "bad_embedding" and i.lesson_id == "L1"]
    assert len(bad) == 1
    assert "empty vector" in bad[0].detail


def test_null_embedding_is_not_flagged_as_bad():
    c = _conn()
    ms.add_lesson(c, "L1", "no embedding stored, and that's fine", None, "i1")

    ok, issues = si.check_store(c)

    assert ok is True
    assert issues == []


def test_bad_embedding_uses_injected_decode_fn_not_real_embeddings_module():
    c = _conn()
    # 8 bytes would be a valid 2-float vector under the real decoder, but the
    # stubbed decoder here expects exactly 4 floats (16 bytes) -> flags it.
    ms.add_lesson(c, "L1", "text", embeddings.to_blob([1.0, 2.0]), "i1")

    ok, issues = si.check_store(c, decode_fn=_fake_decode(vec_len=4))

    assert ok is False
    assert any(i.code == "bad_embedding" and i.lesson_id == "L1" for i in issues)


def test_bad_embedding_passes_with_injected_decode_fn_matching_length():
    c = _conn()
    ms.add_lesson(c, "L1", "text", embeddings.to_blob([1.0, 2.0]), "i1")

    ok, issues = si.check_store(c, decode_fn=_fake_decode(vec_len=2))

    assert ok is True
    assert issues == []


# --- corrupted store: everything at once ----------------------------------------

def test_corrupted_store_reports_all_issue_kinds_and_report_text():
    c = _conn()
    # good lesson
    ms.add_lesson(c, "GOOD", "a perfectly fine lesson", embeddings.to_blob([1.0]), "i0")
    # empty text
    ms.add_lesson(c, "EMPTY", "  ", embeddings.to_blob([1.0]), "i1")
    # malformed embedding
    ms.add_lesson(c, "BADVEC", "text with a broken embedding", b"\x01\x02\x03", "i2")
    # orphan fts: add then delete only the lessons row
    ms.add_lesson(c, "ORPHAN", "will be half-deleted", embeddings.to_blob([1.0]), "i3")
    c.execute("DELETE FROM lessons WHERE id=?", ("ORPHAN",))
    # missing fts: insert into lessons directly, skip the mirror
    c.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction) VALUES(?, ?, ?, ?)",
        ("NOMIRROR", "no fts row for this one", embeddings.to_blob([1.0]), "i4"),
    )
    c.commit()

    ok, issues = si.check_store(c)

    assert ok is False
    codes_present = {i.code for i in issues}
    assert codes_present == {"orphan_fts", "missing_fts", "empty_text", "bad_embedding"}
    ids_by_code = {}
    for i in issues:
        ids_by_code.setdefault(i.code, set()).add(i.lesson_id)
    assert ids_by_code["orphan_fts"] == {"ORPHAN"}
    assert ids_by_code["missing_fts"] == {"NOMIRROR"}
    assert ids_by_code["empty_text"] == {"EMPTY"}
    assert ids_by_code["bad_embedding"] == {"BADVEC"}
    assert "GOOD" not in {lid for ids in ids_by_code.values() for lid in ids}

    report = si.format_report(issues)
    assert report.startswith("4 integrity issue(s) found")
    assert "orphan_fts=1" in report
    assert "missing_fts=1" in report
    assert "empty_text=1" in report
    assert "bad_embedding=1" in report
