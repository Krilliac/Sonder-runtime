import sonder_runtime.adapters.embeddings as e
import memory_store as ms
import retriever as r


def _lesson_outcome(conn, lesson_id, index, signal, value, task="threading lock release"):
    interaction_id = "%s-use-%s" % (lesson_id, index)
    ms.log_lesson_usage(conn, [lesson_id], interaction_id, task)
    ms.record_lesson_usage_outcome(conn, interaction_id, signal, value, source="caller")


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


def test_borderline_cross_domain_hits_cannot_displace_semantic_transfer():
    c = ms.connect(":memory:")
    query = [1.0, 0.0]
    reproduced_false_positive = 0.650211
    semantic_transfer = 0.72
    borderline = [
        reproduced_false_positive,
        (1.0 - reproduced_false_positive ** 2) ** 0.5,
    ]
    transfer = [
        semantic_transfer,
        (1.0 - semantic_transfer ** 2) ** 0.5,
    ]
    ms.add_lesson(
        c,
        "primality",
        "Use the fixed witness set [2,3,...,37] for Miller-Rabin to get a "
        "deterministic primality test for any 64-bit n.",
        e.to_blob(borderline),
        "seed",
    )
    ms.add_lesson(
        c,
        "perf-ipc",
        "Read perf IPC counters before choosing a hot-loop optimization.",
        e.to_blob(borderline),
        "seed",
    )
    ms.add_lesson(
        c,
        "channel-safety",
        "Exercise concurrent endpoint closure, ledger draining, and stale handle reuse.",
        e.to_blob(transfer),
        "seed",
    )

    rows = r.retrieve_with_ids(
        c,
        "Design a deterministic hostile-test matrix for kernel IPC channel "
        "teardown races.",
        k=5,
        embed_fn=lambda _text: query,
        min_sim=0.62,
    )

    assert [row["id"] for row in rows] == ["channel-safety"]
    assert r.retrieve_with_ids(
        c,
        "Design a deterministic hostile-test matrix for kernel IPC channel "
        "teardown races.",
        k=5,
        embed_fn=lambda _text: None,
    ) == []


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
        ms.record_lesson_usage_outcome(conn, interaction_id, "failed", -1.0, source="caller")

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


def test_retrieve_mmr_diversifies_near_duplicates(monkeypatch):
    c = ms.connect(":memory:")
    # dup1/dup2 are near-identical and most query-aligned; distinct is a
    # weaker but genuinely different lesson. Plain relevance truncation at
    # k=2 would pick both duplicates; MMR must swap one for the distinct.
    ms.add_lesson(c, "dup1", "use pathlib for path joins",
                  e.to_blob([0.9, 0.436, 0.0]), "i")
    ms.add_lesson(c, "dup2", "use pathlib for joining paths",
                  e.to_blob([0.89, 0.44, 0.06]), "i")
    ms.add_lesson(c, "distinct", "cache embeddings by revision",
                  e.to_blob([0.8, -0.6, 0.0]), "i")
    rows = r.retrieve_with_ids(
        c, "query", k=2, embed_fn=lambda t: [1.0, 0.0, 0.0], min_sim=0.1,
    )
    picked = [row["id"] for row in rows]
    assert picked[0] == "dup1"
    assert "distinct" in picked

    # SONDER_MMR_LAMBDA=1 restores pure relevance order (both duplicates).
    monkeypatch.setenv("SONDER_MMR_LAMBDA", "1")
    rows = r.retrieve_with_ids(
        c, "query", k=2, embed_fn=lambda t: [1.0, 0.0, 0.0], min_sim=0.1,
    )
    assert [row["id"] for row in rows] == ["dup1", "dup2"]


# --- Quarantine evidence: shared blame, reference class, and the exit ------


def _graded(conn, interaction_id, lesson_ids, task, reward):
    ms.log_lesson_usage(conn, lesson_ids, interaction_id, task)
    ms.record_lesson_usage_outcome(
        conn, interaction_id, "tests_passed" if reward > 0 else "failed", reward, source="caller",
    )


def test_shared_blame_does_not_count_as_independent_evidence():
    """record_lesson_usage_outcome writes one task's reward onto EVERY lesson
    retrieved for it, so loss rows are not independent failures. Measured on the
    live store 2026-08-06: 493 loss rows trace to only 144 distinct failing
    interactions (mean cohort 3.424), and four of the six quarantined lessons
    crossed the five-loss threshold on the IDENTICAL five interactions -- one
    cluster counted four times. Blame is split across the cohort, so four
    co-retrieved lessons sharing six failures hold 1.5 attributable losses each
    and stay retrievable, while a lesson that fails alone the same number of
    times holds 6.0 and is still caught."""
    conn = ms.connect(":memory:")
    cohort = ["co%d" % index for index in range(4)]
    for lesson_id in cohort + ["solo"]:
        ms.add_lesson(conn, lesson_id, "threading lock advice %s" % lesson_id,
                      None, "seed")
    for index in range(6):
        _graded(conn, "shared-%d" % index, cohort, "task %d" % index, -1.0)
        _graded(conn, "solo-%d" % index, ["solo"], "task %d" % index, -1.0)

    stats = r.usage_stats_with_attribution(conn)
    shared = r.lesson_quarantine(stats["co0"])
    solo = r.lesson_quarantine(stats["solo"])

    assert shared["losses_since_win"] == solo["losses_since_win"] == 6
    assert shared["attributable_losses_since_win"] == 1.5
    assert solo["attributable_losses_since_win"] == 6.0
    assert shared["active"] is False
    assert solo["active"] is True


def test_quarantine_judges_a_lesson_against_its_own_frequency_band():
    """Loss rate is set by retrieval frequency, not lesson quality: measured over
    9256 scored retrievals, lessons retrieved once lose 50.62% of the time and
    lessons retrieved 100+ times lose 1.24%, against a 5.33% corpus rate. A
    corpus-wide threshold therefore quarantines lessons for being unusual. These
    two lessons carry IDENTICAL epoch evidence -- six failures at a cohort of
    three, 2.0 attributable losses each -- and differ only in lifetime retrieval
    count. Six losses is an unremarkable run at the 13.70% base rate of the 5-9
    band (p=0.019) and a real anomaly at the 7.59% rate of the 10-24 band
    (p=0.006), so only the frequently-retrieved one is quarantined."""
    conn = ms.connect(":memory:")
    for lesson_id in ["rare", "common", "filler"]:
        ms.add_lesson(conn, lesson_id, "threading lock advice %s" % lesson_id,
                      None, "seed")
    # "common" banks six wins first, lifting it into the next frequency band.
    # A win also resets the epoch, so both lessons enter the losses identically.
    for index in range(6):
        _graded(conn, "win-%d" % index, ["common"], "good task %d" % index, 1.0)
    for index in range(6):
        _graded(conn, "fail-%d" % index, ["rare", "common", "filler"],
                "bad task %d" % index, -1.0)

    stats = r.usage_stats_with_attribution(conn)
    rare = r.lesson_quarantine(stats["rare"])
    common = r.lesson_quarantine(stats["common"])

    assert rare["losses_since_win"] == common["losses_since_win"] == 6
    assert round(rare["attributable_losses_since_win"], 6) == 2.0
    assert round(common["attributable_losses_since_win"], 6) == 2.0
    assert rare["scored_retrievals"] == 6
    assert common["scored_retrievals"] == 12
    assert rare["band_loss_rate"] == 0.1370
    assert common["band_loss_rate"] == 0.0759
    assert rare["active"] is False
    assert common["active"] is True


def test_quarantine_still_fires_on_an_unambiguously_harmful_lesson():
    """The discount must not make quarantine unfireable. A lesson that fails
    alone -- the fully identifiable case, 18 of the 144 live failing interactions
    blamed exactly one lesson -- still crosses every gate, and retrieval drops it
    while an untested peer with the same text is returned."""
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "harmful", "threading lock release advice", None, "seed")
    ms.add_lesson(conn, "peer", "threading lock release guidance", None, "seed")
    for index in range(6):
        _graded(conn, "harmful-%d" % index, ["harmful"],
                "threading lock task %d" % index, -1.0)

    decision = r.lesson_quarantine(r.usage_stats_with_attribution(conn)["harmful"])
    rows = r.retrieve_with_ids(
        conn, "threading lock release", k=2, embed_fn=lambda _text: None,
    )

    assert decision["active"] is True
    assert decision["attribution_source"] == "measured"
    assert [row["id"] for row in rows] == ["peer"]


def test_quarantine_reaches_probation_and_a_win_is_the_exit():
    """Quarantine excluded a lesson from retrieval, so the win that would clear
    it was unreachable and only the seven-day timer could lift it -- one live
    quarantined lesson has 18 lifetime wins and no way to earn a 19th. A
    quarantined lesson is now hard-excluded for the first day, then admitted on a
    deterministic ~1-in-20 sample of tasks, and a grounded win on one of those
    probation retrievals ends the quarantine on evidence rather than on a clock."""
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "threading lock release lesson", None, "seed")
    for index in range(6):
        _graded(conn, "fail-%d" % index, ["lesson"],
                "threading lock task %d" % index, -1.0)

    admitted = next(
        task for task in ("threading lock release %d" % n for n in range(200))
        if r._probation_admits("lesson", task)
    )
    # Fresh quarantine: excluded even for a task probation would sample.
    assert r.retrieve_with_ids(
        conn, admitted, embed_fn=lambda _text: None,
    ) == []

    conn.execute(
        "UPDATE lesson_usage SET ts=datetime('now', '-2 days'), "
        "outcome_ts=datetime('now', '-2 days') WHERE lesson_id='lesson'"
    )
    conn.commit()
    stats = r.usage_stats_with_attribution(conn)
    assert r.lesson_quarantine(stats["lesson"])["active"] is True
    assert r.lesson_quarantine(stats["lesson"])["probation_eligible"] is True

    # Still quarantined, but reachable on the sampled slice of traffic.
    refused = next(
        task for task in ("threading lock release %d" % n for n in range(200))
        if not r._probation_admits("lesson", task)
    )
    assert r.retrieve_with_ids(conn, refused, embed_fn=lambda _text: None) == []
    probation = r.retrieve_with_ids(conn, admitted, embed_fn=lambda _text: None)
    assert [row["id"] for row in probation] == ["lesson"]

    _graded(conn, "probation-win", ["lesson"], admitted, 1.0)

    assert r.lesson_quarantine(
        r.usage_stats_with_attribution(conn)["lesson"]
    )["active"] is False
    assert [row["id"] for row in r.retrieve_with_ids(
        conn, refused, embed_fn=lambda _text: None,
    )] == ["lesson"]


def test_probation_sampling_is_deterministic_and_bounded():
    """Probation has to be reproducible -- the same task must always make the
    same call, or an A/B of retrieval becomes unreplayable -- and it has to stay
    a slice, since the lesson may genuinely be harmful. ~1-in-20 bounds exposure
    far below the status quo, where the cooldown restored a lesson to 100% of
    traffic after a week on no evidence at all."""
    tasks = ["threading lock task %d" % index for index in range(2000)]

    assert all(
        r._probation_admits("lesson", task) == r._probation_admits("lesson", task)
        for task in tasks[:50]
    )
    # Different lessons draw independently on the same task.
    assert any(
        r._probation_admits("a", task) != r._probation_admits("b", task)
        for task in tasks[:50]
    )
    rate = sum(r._probation_admits("lesson", task) for task in tasks) / len(tasks)
    assert 0.02 < rate < 0.10


def test_unenriched_stats_report_an_undiscounted_upper_bound():
    """lesson_quarantine takes a stats row, and a row does not say which lessons
    shared each failure, so callers that cannot enrich it (learning_health passes
    memory_store.lesson_usage_stats rows straight through) get the raw count. The
    fallback is deliberately the UPPER bound rather than the measured mean cohort
    of 3.424: assuming the mean would divide a solo failure's evidence by 3.4,
    and a lesson failing alone is the most identifiable and most likely genuinely
    harmful. Retrieval, which holds the connection, applies the real discount."""
    conn = ms.connect(":memory:")
    cohort = ["co%d" % index for index in range(4)]
    for lesson_id in cohort:
        ms.add_lesson(conn, lesson_id, "threading lock advice %s" % lesson_id,
                      None, "seed")
    for index in range(6):
        _graded(conn, "shared-%d" % index, cohort, "task %d" % index, -1.0)

    unenriched = r.lesson_quarantine(ms.lesson_usage_stats(conn)["co0"])
    enriched = r.lesson_quarantine(r.usage_stats_with_attribution(conn)["co0"])

    assert unenriched["attribution_source"] == "unattributed"
    assert unenriched["attributable_losses_since_win"] == 6.0
    assert unenriched["active"] is True
    assert enriched["attribution_source"] == "measured"
    assert enriched["attributable_losses_since_win"] == 1.5
    assert enriched["active"] is False
    # Retrieval is the decision surface and uses the discounted view.
    assert len(r.retrieve_with_ids(
        conn, "threading lock advice", k=4, embed_fn=lambda _text: None,
    )) == 4


def test_usage_boost_confidence_comes_from_scored_outcomes_not_retrievals():
    """_usage_boost weighted its tiebreak by `uses`, which counts retrievals that
    were never graded. Measured 2026-08-06: 157 of 385 retrieved lessons have
    uses != scored, 39 have retrievals but zero scored outcomes, and 63 had their
    weight inflated by ungraded retrievals -- one lesson with a single win and
    100 retrievals drew the same full confidence as a lesson with 100 graded
    wins. Weight now shrinks toward the neutral prior on SCORED evidence, so an
    asserted-but-never-validated lesson sits at 0.0, below any earned record.

    The counts are per-population (#25): the boost reads the deciding
    population's own mean and its own scored count, so the fixtures name that
    population rather than the blended `avg_reward`/`wins`/`losses` triple.
    The weighting under test is unchanged -- 50 scored rows still shrink to
    50/60, one still shrinks to 1/11."""
    earned = r._usage_boost({"uses": 50, "avg_reward_caller": 1.0,
                             "scored_caller": 50})
    thin = r._usage_boost({"uses": 100, "avg_reward_caller": 1.0,
                           "scored_caller": 1})
    synthetic = r._usage_boost(None)
    never_scored = r._usage_boost({"uses": 100, "avg_reward_caller": None,
                                   "scored_caller": 0,
                                   "avg_reward_execution": None,
                                   "scored_execution": 0})

    assert synthetic == 0.0
    assert never_scored == 0.0
    # 100 ungraded retrievals no longer buy the confidence of one graded win.
    assert thin < earned
    assert round(thin, 6) == round(1.0 * (1 / 11.0) * 0.01, 6)
    assert round(earned, 6) == round(1.0 * (50 / 60.0) * 0.01, 6)
    # Still a gentle tiebreaker, never a relevance override.
    assert -0.01 <= thin <= 0.01 and -0.01 <= earned <= 0.01
    # An earned positive record outranks an unvalidated lesson; an earned
    # negative one ranks below it.
    assert earned > synthetic > r._usage_boost(
        {"uses": 50, "avg_reward_caller": -1.0, "scored_caller": 50}
    )


def test_attribution_scans_the_usage_history_once_per_call():
    """retrieve_with_ids calls this before every generation, and the epoch
    reducer and the blame reducer used to issue byte-identical ordered scans of
    lesson_usage -- the whole table sorted twice per turn (52 ms per scan over
    the live 11k rows) for one row sequence. The shares themselves must not
    move: the cohort is now counted from those same rows instead of a second
    GROUP BY."""
    conn = ms.connect(":memory:")
    cohort = ["co%d" % index for index in range(4)]
    for lesson_id in cohort + ["solo"]:
        ms.add_lesson(conn, lesson_id, "advice %s" % lesson_id, None, "seed")
    for index in range(6):
        _graded(conn, "shared-%d" % index, cohort, "task %d" % index, -1.0)
        _graded(conn, "solo-%d" % index, ["solo"], "task %d" % index, -1.0)

    scans = []
    conn.set_trace_callback(lambda sql: scans.append(" ".join(sql.split())))
    stats = r.usage_stats_with_attribution(conn)
    conn.set_trace_callback(None)

    ordered = [sql for sql in scans if "ORDER BY lesson_id" in sql]
    grouped = [sql for sql in scans if "GROUP BY" in sql]
    assert len(ordered) == 1, ordered
    assert len(grouped) == 1, grouped
    assert stats["co0"]["attributable_losses_since_win"] == 1.5
    assert stats["solo"]["attributable_losses_since_win"] == 6.0


def _judged(conn, lesson_id, index, signal, reward, task="threading lock release"):
    """One graded retrieval of one lesson, carrying its outcome POPULATION."""
    interaction_id = "%s-j%s" % (lesson_id, index)
    ms.log_lesson_usage(conn, [lesson_id], interaction_id, task)
    ms.record_lesson_usage_outcome(conn, interaction_id, signal, reward)


def test_the_usage_boost_orders_rows_so_it_may_not_read_the_blended_mean():
    """#25: `_usage_boost` is added into the retrieval sort key, which makes it
    a BETWEEN-ROW ordering, not a per-lesson boost -- and it read `avg_reward`,
    the mean over both outcome populations that `lesson_usage_stats` documents
    as one that "must never ORDER two lessons against each other".

    Probe-measured on this branch: a lesson a caller REJECTED (-0.5) whose
    runtime then passed its own tests eight times blends to +0.833 and takes a
    +0.0039 boost, beating a lesson a caller reviewed and ACCEPTED (+0.8) at
    +0.0007. The self-graded rows launder the caller's judgement. The cap is
    0.01 against an adjacent-rank RRF gap of 0.000264, so this moves a row up
    to 37 ranks -- it is not a tiebreaker in any bounded sense.
    """
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "reviewed", "threading lock release advice reviewed",
                  None, "seed")
    ms.add_lesson(conn, "laundered", "threading lock release advice laundered",
                  None, "seed")

    _judged(conn, "reviewed", 0, "accepted", 0.8)
    _judged(conn, "laundered", 0, "rejected", -0.5)
    for index in range(1, 9):
        _judged(conn, "laundered", index, "tests_passed", 1.0)

    stats = ms.lesson_usage_stats(conn)
    assert stats["laundered"]["avg_reward"] > stats["reviewed"]["avg_reward"], (
        "precondition: the blend really does favour the rejected lesson"
    )

    assert r._usage_boost(stats["reviewed"]) > r._usage_boost(stats["laundered"])
    rows = r.retrieve_with_ids(conn, "threading lock release advice", k=2,
                               embed_fn=lambda _text: None)
    assert [row["id"] for row in rows] == ["reviewed", "laundered"]


def test_the_usage_boost_confidence_comes_from_the_deciding_population():
    """The magnitude and the certainty must come from the SAME population.

    Reading the caller mean while weighting it by every scored row lets one
    caller judgement borrow the confidence of a hundred self-graded ones --
    the same blend, moved into the second factor. One caller rejection is
    thin evidence and must be weighted as thin evidence.
    """
    one_caller_row_among_many = {
        "uses": 101, "wins": 100, "losses": 1,
        "avg_reward_caller": -0.5, "scored_caller": 1,
        "avg_reward_execution": 1.0, "scored_execution": 100,
    }
    one_caller_row_alone = {
        "uses": 1, "wins": 0, "losses": 1,
        "avg_reward_caller": -0.5, "scored_caller": 1,
        "avg_reward_execution": None, "scored_execution": 0,
    }

    assert (
        r._usage_boost(one_caller_row_among_many)
        == r._usage_boost(one_caller_row_alone)
    ), "self-graded volume must not lend certainty to a caller's judgement"
    assert round(r._usage_boost(one_caller_row_alone), 6) == round(
        -0.5 * (1 / 11.0) * 0.01, 6
    )


def test_the_execution_mean_decides_only_when_no_caller_has_judged():
    """A lesson no caller has looked at still has evidence; it is just weaker
    evidence, and it decides nothing once a caller has spoken."""
    self_graded_only = {
        "uses": 10, "wins": 10, "losses": 0,
        "avg_reward_caller": None, "scored_caller": 0,
        "avg_reward_execution": 1.0, "scored_execution": 10,
    }
    unjudged = {
        "uses": 10, "wins": 0, "losses": 0,
        "avg_reward_caller": None, "scored_caller": 0,
        "avg_reward_execution": None, "scored_execution": 0,
    }

    assert r._usage_boost(self_graded_only) > 0.0
    assert r._usage_boost(unjudged) == 0.0
    # An unattributable outcome (a signal in neither population) buys nothing:
    # unknown provenance ranks below both, as it does in the rules.
    assert r._usage_boost({"uses": 5, "wins": 5, "losses": 0,
                           "avg_reward": 1.0}) == 0.0


def test_lesson_usage_stats_counts_each_population_separately():
    """The split counts have to come from the store beside the split means --
    a ranking that reads one population's mean needs that population's own
    count, and guessing it downstream is how the blend comes back."""
    conn = ms.connect(":memory:")
    ms.add_lesson(conn, "lesson", "advice", None, "seed")
    _judged(conn, "lesson", 0, "rejected", -0.5)
    for index in range(1, 4):
        _judged(conn, "lesson", index, "tests_passed", 1.0)

    row = ms.lesson_usage_stats(conn)["lesson"]

    assert row["scored_caller"] == 1
    assert row["scored_execution"] == 3
    assert row["avg_reward_caller"] == -0.5
    assert row["avg_reward_execution"] == 1.0
