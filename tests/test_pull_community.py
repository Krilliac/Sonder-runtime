import sonder_runtime.adapters.embeddings as embeddings
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


def test_merge_lessons_rejects_private_text_before_embedding_or_fts():
    c = _conn()
    embedded = []

    added = pc.merge_lessons(
        c,
        [{"id": "unsafe", "text": "Authorization: Token glpat-abcdefghijklmnop"}],
        embed_fn=lambda text: embedded.append(text) or [1.0, 0.0],
    )

    assert added == 0
    assert embedded == []
    assert ms.all_lessons(c) == []
    assert c.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0] == 0


def test_merge_lessons_rejects_vague_platitudes_before_embedding():
    c = _conn()
    embedded = []
    added = pc.merge_lessons(
        c,
        [{"id": "vague", "text": "Use data structures efficiently."}],
        embed_fn=lambda text: embedded.append(text) or [1.0, 0.0],
    )
    assert added == 0
    assert embedded == []
    assert ms.all_lessons(c) == []


def test_merge_lessons_applies_embedding_similarity_dedup(monkeypatch):
    c = _conn()
    ms.add_lesson(c, "existing", "Use bisect for ordered insertion.",
                  embeddings.to_blob([1.0, 0.0]), "grounded")
    monkeypatch.setattr(pc.reflection, "is_duplicate", lambda *args, **kwargs: True)
    added = pc.merge_lessons(
        c,
        [{"id": "similar", "text": "Use bisect_left to retain sorted order."}],
        embed_fn=lambda _text: [1.0, 0.0],
    )
    assert added == 0
    assert len(ms.all_lessons(c)) == 1


def test_merge_lessons_skips_non_object_lines_and_keeps_later_lessons():
    c = _conn()
    added = pc.merge_lessons(
        c,
        ["some text", [1], 7, None,
         {"id": "c1", "text": "Prefer early returns over deep nesting."}],
    )
    assert added == 1
    assert [row["text"] for row in ms.all_lessons(c)] == [
        "Prefer early returns over deep nesting."
    ]


def test_merge_lessons_does_not_reintroduce_tombstoned_text():
    c = _conn()
    ms.add_lesson(c, "pruned", "Cache expensive pure function calls.", None, "int1")
    assert ms.tombstone_lesson(c, "pruned")
    assert ms.all_lessons(c) == []

    added = pc.merge_lessons(
        c, [{"id": "c1", "text": "  cache expensive PURE function calls. "}],
    )

    assert added == 0
    assert ms.all_lessons(c) == []


def test_merge_lessons_does_not_reintroduce_semantic_tombstone(monkeypatch):
    c = _conn()
    provenance = {"model": "test-embed", "revision": "r1", "dimension": 2}
    ms.add_lesson(
        c, "pruned", "Use bisect for ordered insertion.",
        embeddings.to_blob([1.0, 0.0]), "int1",
        embedding_model="test-embed", embedding_revision="r1", embedding_dim=2,
    )
    assert ms.tombstone_lesson(c, "pruned")
    monkeypatch.setattr(
        pc.embeddings, "trusted_provenance", lambda *_a, **_k: dict(provenance),
    )

    added = pc.merge_lessons(
        c,
        [{"id": "similar", "text": "Use bisect_left to retain sorted order."}],
        embed_fn=lambda _text: [1.0, 0.0],
    )

    assert added == 0
    assert ms.all_lessons(c) == []
