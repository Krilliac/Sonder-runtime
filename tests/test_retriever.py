import embeddings as e
import memory_store as ms
import retriever as r


def _lesson_outcome(conn, lesson_id, index, signal, value, task="threading lock release"):
    interaction_id = "%s-use-%s" % (lesson_id, index)
    ms.log_lesson_usage(conn, [lesson_id], interaction_id, task)
    ms.record_lesson_usage_outcome(conn, interaction_id, signal, value)


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


def test_retrieve_drops_unembedded_lexical_candidates_when_compatible_corpus_exists():
    c = ms.connect(":memory:")
    # Lexically matches "threading lock" but has no embedding to judge relevance by
    # -> must be dropped from the thresholded path when a compatible semantic
    # corpus exists, even though FTS surfaces it.
    ms.add_lesson(c, "no-embedding", "always release the threading lock", None, "i")
    ms.add_lesson(
        c, "compatible-corpus", "unrelated semantic candidate",
        e.to_blob([0.0, 1.0]), "i",
    )
    texts = r.retrieve(c, "threading lock release", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert texts == []


def test_retrieve_embed_fn_none_still_uses_lexical_fallback_with_min_sim_set():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    texts = r.retrieve(
        c, "threading lock release", embed_fn=lambda t: None, min_sim=0.9
    )
    assert any("threading lock" in t for t in texts)


def test_retrieve_falls_back_to_lexical_when_stored_vector_dimension_changed():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "old-model", "always release the threading lock",
        e.to_blob([1.0, 0.0]), "i",
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release",
        embed_fn=lambda _text: [1.0, 0.0, 0.0], min_sim=0.9,
    )

    assert [row["id"] for row in rows] == ["old-model"]


def test_retrieve_skips_incompatible_vectors_when_compatible_corpus_exists():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "old-model", "threading lock release old vector",
        e.to_blob([1.0, 0.0]), "i",
    )
    ms.add_lesson(
        c, "current-model", "threading lock release current vector",
        e.to_blob([1.0, 0.0, 0.0]), "i",
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=2,
        embed_fn=lambda _text: [1.0, 0.0, 0.0], min_sim=0.9,
    )

    assert [row["id"] for row in rows] == ["current-model"]


def test_retrieve_skips_wrong_model_vector_even_when_dimension_matches():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "old-model", "threading lock release old model",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v1",
    )
    ms.add_lesson(
        c, "current-model", "threading lock release current model",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v2",
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=2,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2",
    )

    assert [row["id"] for row in rows] == ["current-model"]


def test_retrieve_rejects_vector_with_corrupt_dimension_metadata():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "bad-metadata", "threading lock release bad metadata",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v2",
        embedding_dim=2,
    )
    c.execute("UPDATE lessons SET embedding_dim=3 WHERE id='bad-metadata'")
    c.commit()
    ms.add_lesson(
        c, "valid", "threading lock release valid metadata",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v2",
        embedding_dim=2,
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=2,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2",
    )

    assert [row["id"] for row in rows] == ["valid"]


def test_missing_dimension_metadata_cannot_suppress_lexical_fallback():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "missing-dimension", "threading lock release current model",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v2",
        embedding_revision="rev-v2", embedding_dim=2,
    )
    c.execute(
        "UPDATE lessons SET embedding_dim=NULL WHERE id='missing-dimension'"
    )
    c.commit()

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=1,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=1.1,
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )

    assert [row["id"] for row in rows] == ["missing-dimension"]


def test_unversioned_runtime_rejects_hashed_stored_revision():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "stale-revision", "threading lock release stale revision",
        e.to_blob([1.0, 0.0]), "i", embedding_model="embed-v2",
        embedding_revision="stale-hash", embedding_dim=2,
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=1,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=1.1,
        embedding_model="embed-v2", embedding_revision="",
    )

    assert [row["id"] for row in rows] == ["stale-revision"]


def test_zero_norm_vector_cannot_suppress_lexical_fallback():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "zero", "threading lock release zero vector",
        e.to_blob([1.0, 0.0]), "i", embedding_dim=2,
    )
    c.execute(
        "UPDATE lessons SET embedding=? WHERE id='zero'",
        (e.to_blob([0.0, 0.0]),),
    )
    c.commit()

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=1,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=1.1,
    )

    assert [row["id"] for row in rows] == ["zero"]


def test_only_quarantined_compatible_vectors_do_not_block_lexical_fallback():
    c = ms.connect(":memory:")
    ms.add_lesson(
        c, "quarantined-current", "threading lock release current vector",
        e.to_blob([1.0, 0.0, 0.0]), "i",
    )
    for index in range(r.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
        _lesson_outcome(c, "quarantined-current", index, "failed", -1.0)
    ms.add_lesson(
        c, "old-model", "threading lock release old vector",
        e.to_blob([1.0, 0.0]), "i",
    )

    rows = r.retrieve_with_ids(
        c, "threading lock release", k=2,
        embed_fn=lambda _text: [1.0, 0.0, 0.0], min_sim=0.9,
    )

    assert [row["id"] for row in rows] == ["old-model"]


def test_retrieve_with_ids_returns_ids_and_text():
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "always release the threading lock", None, "i")
    rows = r.retrieve_with_ids(c, "threading lock release", embed_fn=lambda t: None)
    assert rows[0]["id"] == "L1"
    assert "threading lock" in rows[0]["text"]


def test_retrieve_excludes_repeated_unanimous_failure_lesson():
    conn = ms.connect(":memory:")
    vector = e.to_blob([1.0, 0.0])
    ms.add_lesson(
        conn, "Z-bad", "threading lock release without validation", vector, "seed",
    )
    ms.add_lesson(
        conn, "A-good", "threading lock release with a context manager", vector, "seed",
    )
    for index in range(r.QUARANTINE_MIN_LOSSES):
        _lesson_outcome(
            conn, "Z-bad", index, "failed", -1.0,
            task="threading lock release variant %s" % index,
        )

    semantic = r.retrieve_with_ids(
        conn, "threading lock release", k=1,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=0.5,
    )
    lexical = r.retrieve_with_ids(
        conn, "threading lock release", k=1, embed_fn=lambda _text: None,
    )

    assert [row["id"] for row in semantic] == ["A-good"]
    assert [row["id"] for row in lexical] == ["A-good"]


def test_retrieve_keeps_cold_and_single_failure_lessons():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "cold", "threading lock cold lesson", None, "seed")
    ms.add_lesson(conn, "one-loss", "threading lock one loss lesson", None, "seed")
    _lesson_outcome(conn, "one-loss", 0, "failed", -1.0)

    rows = r.retrieve_with_ids(
        conn, "threading lock lesson", k=2, embed_fn=lambda _text: None,
    )

    assert {row["id"] for row in rows} == {"cold", "one-loss"}


def test_positive_outcome_rehabilitates_quarantined_lesson():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "threading lock release lesson", None, "seed")
    for index in range(r.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
        _lesson_outcome(conn, "lesson", index, "failed", -1.0)
    assert r.retrieve_with_ids(
        conn, "threading lock release", embed_fn=lambda _text: None,
    ) == []

    # Cooldown creates a real production probation path instead of requiring a
    # direct write to a lesson that retrieval can never select.
    conn.execute(
        "UPDATE lesson_usage SET ts=datetime('now', '-8 days'), "
        "outcome_ts=datetime('now', '-8 days') "
        "WHERE lesson_id='lesson'"
    )
    conn.commit()
    probation = r.retrieve_with_ids(
        conn, "threading lock release", embed_fn=lambda _text: None,
    )
    assert [row["id"] for row in probation] == ["lesson"]

    _lesson_outcome(conn, "lesson", "success", "tests_passed", 1.0)

    rows = r.retrieve_with_ids(
        conn, "threading lock release", embed_fn=lambda _text: None,
    )
    assert [row["id"] for row in rows] == ["lesson"]


def test_lesson_can_relapse_after_historical_success():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "threading lock release lesson", None, "seed")
    _lesson_outcome(conn, "lesson", "old-win", "tests_passed", 1.0)
    for index in range(r.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
        _lesson_outcome(conn, "lesson", index, "failed", -1.0)

    assert r.retrieve_with_ids(
        conn, "threading lock release", embed_fn=lambda _text: None,
    ) == []


def test_delayed_failures_start_cooldown_when_feedback_arrives():
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "threading lock release lesson", None, "seed")
    interaction_ids = []
    for index in range(r.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
        interaction_id = "lesson-delayed-%s" % index
        interaction_ids.append(interaction_id)
        ms.log_lesson_usage(
            conn, ["lesson"], interaction_id, "threading lock release",
        )
    conn.execute(
        "UPDATE lesson_usage SET ts=datetime('now', '-8 days') "
        "WHERE lesson_id='lesson'"
    )
    conn.commit()

    for interaction_id in interaction_ids:
        ms.record_lesson_usage_outcome(conn, interaction_id, "failed", -1.0)

    decision = r.lesson_quarantine(ms.lesson_usage_stats(conn)["lesson"])
    assert decision["active"] is True
    assert r.retrieve_with_ids(
        conn, "threading lock release", embed_fn=lambda _text: None,
    ) == []


def test_quarantined_lexical_hits_do_not_starve_valid_candidates():
    conn = ms.connect(":memory:")
    for lesson_index in range(12):
        lesson_id = "bad-%02d" % lesson_index
        ms.add_lesson(
            conn, lesson_id, "threading lock release common", None, "seed",
        )
        for use_index in range(r.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
            _lesson_outcome(conn, lesson_id, use_index, "failed", -1.0)
    ms.add_lesson(conn, "good-a", "threading lock release common", None, "seed")
    ms.add_lesson(conn, "good-b", "threading lock release common", None, "seed")

    rows = r.retrieve_with_ids(
        conn, "threading lock release", k=2, embed_fn=lambda _text: None,
    )

    assert {row["id"] for row in rows} == {"good-a", "good-b"}


def test_retrieval_batches_candidate_lookups():
    conn = ms.connect(":memory:")
    vector = e.to_blob([1.0, 0.0])
    for index in range(200):
        ms.add_lesson(
            conn, "lesson-%03d" % index,
            "threading lock release candidate %03d" % index,
            vector,
            "seed",
        )
    selects = []
    conn.set_trace_callback(
        lambda statement: selects.append(statement)
        if statement.lstrip().upper().startswith("SELECT") else None
    )

    rows = r.retrieve_with_ids(
        conn, "threading lock release", k=20,
        embed_fn=lambda _text: [1.0, 0.0], min_sim=0.5,
    )

    conn.set_trace_callback(None)
    assert len(rows) == 20
    assert len(selects) <= 8
