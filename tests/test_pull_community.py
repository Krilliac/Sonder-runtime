import embeddings
import memory_store as ms
import pull_community as pc


def _conn():
    return ms.connect(":memory:")


def test_merge_lessons_adds_only_new():
    c = _conn()
    ms.add_lesson(c, "existing1", "Use a set for O(1) membership tests.", None, "int1")

    community = [
        {"id": "c1", "text": "Prefer early returns over deep nesting."},
        {"id": "c2", "text": "Cache expensive pure function calls."},
        {"id": "c3", "text": "Use a set for O(1) membership tests."},  # duplicate of existing1
    ]

    added = pc.merge_lessons(c, community)

    assert added == 2
    texts = {lesson["text"] for lesson in ms.all_lessons(c)}
    assert "Prefer early returns over deep nesting." in texts
    assert "Cache expensive pure function calls." in texts


def test_merge_lessons_sets_community_source_and_none_embedding():
    c = _conn()
    pc.merge_lessons(c, [{"id": "c1", "text": "Prefer early returns over deep nesting."}])

    row = c.execute(
        "SELECT source_interaction, embedding FROM lessons WHERE text=?",
        ("Prefer early returns over deep nesting.",),
    ).fetchone()

    assert row["source_interaction"] == "community"
    assert row["embedding"] is None


def test_merge_lessons_dedupe_case_and_whitespace_insensitive():
    c = _conn()
    ms.add_lesson(c, "existing1", "  Use A Set for O(1) membership tests.  ", None, "int1")

    added = pc.merge_lessons(c, [{"id": "c1", "text": "use a set for o(1) membership tests."}])

    assert added == 0


def test_merge_lessons_keeps_legacy_blob_returning_embed_callback():
    c = _conn()
    blob = embeddings.to_blob([1.0, 0.0])

    added = pc.merge_lessons(
        c,
        [{"id": "c1", "text": "Keep callback compatibility."}],
        embed_fn=lambda _text: blob,
    )

    row = c.execute(
        "SELECT embedding, embedding_model FROM lessons WHERE text=?",
        ("Keep callback compatibility.",),
    ).fetchone()
    assert added == 1
    assert row["embedding"] == blob
    assert row["embedding_model"] is None
