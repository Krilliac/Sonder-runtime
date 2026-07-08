import embeddings as e
import memory_store as ms
import retriever as r


def test_rrf_rewards_agreement():
    # B appears in BOTH lists; A appears in only one (even at rank 0).
    # Standard RRF rewards cross-list agreement, so B wins.
    fused = r.rrf([["A", "B"], ["B", "C"]])
    assert fused[0] == "B"


def test_semantic_search_uses_embeddings():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "near", "x", e.to_blob([1.0, 0.0]), "i")
    ms.add_lesson(c, "far", "y", e.to_blob([0.0, 1.0]), "i")
    hits = r.semantic_search(c, "query", embed_fn=lambda t: [0.9, 0.1])
    assert hits[0] == "near"


def test_semantic_search_empty_when_no_embeddings():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "near", "x", e.to_blob([1.0, 0.0]), "i")
    assert r.semantic_search(c, "q", embed_fn=lambda t: None) == []


def test_retrieve_returns_texts_and_degrades_to_lexical():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    ms.add_lesson(c, "L2", "prefer RRF for hybrid ranking", None, "i")
    # embeddings unavailable -> lexical only, still finds the lock lesson.
    texts = r.retrieve(c, "threading lock release", embed_fn=lambda t: None)
    assert any("threading lock" in t for t in texts)


def test_retrieve_filters_out_lessons_below_min_sim():
    c = ms.connect(":memory:")
    # "near" is aligned with the query vector; "far" is orthogonal.
    ms.add_lesson(c, "near", "on-topic lesson", e.to_blob([1.0, 0.0]), "i")
    ms.add_lesson(c, "far", "off-topic lesson", e.to_blob([0.0, 1.0]), "i")
    texts = r.retrieve(c, "query", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert texts == ["on-topic lesson"]


def test_retrieve_returns_empty_when_all_candidates_below_min_sim():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "near", "on-topic lesson", e.to_blob([1.0, 0.0]), "i")
    ms.add_lesson(c, "far", "off-topic lesson", e.to_blob([0.0, 1.0]), "i")
    # min_sim above even the best cosine (1.0 is the max) -> nothing clears the bar.
    texts = r.retrieve(c, "query", embed_fn=lambda t: [1.0, 0.0], min_sim=1.1)
    assert texts == []


def test_retrieve_drops_lexical_candidates_with_no_stored_embedding():
    c = ms.connect(":memory:")
    # Lexically matches "threading lock" but has no embedding to judge relevance by
    # -> must be dropped from the thresholded path even though FTS surfaces it.
    ms.add_lesson(c, "no-embedding", "always release the threading lock", None, "i")
    texts = r.retrieve(c, "threading lock release", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert texts == []


def test_retrieve_embed_fn_none_still_uses_lexical_fallback_with_min_sim_set():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    texts = r.retrieve(
        c, "threading lock release", embed_fn=lambda t: None, min_sim=0.9
    )
    assert any("threading lock" in t for t in texts)


def test_retrieve_with_ids_returns_ids_and_text():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    rows = r.retrieve_with_ids(c, "threading lock release", embed_fn=lambda t: None)
    assert rows[0]["id"] == "L1"
    assert "threading lock" in rows[0]["text"]
