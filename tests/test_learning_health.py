import learning_health
import memory_store


def _conn():
    return memory_store.connect(":memory:")


def _interaction(conn, interaction_id):
    memory_store.log_interaction(
        conn,
        interaction_id,
        "task",
        "",
        "answer",
        "code",
    )


def test_empty_learning_store_is_building():
    conn = _conn()
    try:
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["status"] == "building"
    assert report["outcome_coverage_percent"] == 0.0
    assert report["positive_percent"] == 0.0
    assert report["distillation_yield"] is None
    assert report["signals"] == []
    assert report["interaction_task_embeddings"]["interactions"] == 0


def test_learning_health_exposes_raw_interaction_embedding_maintenance():
    conn = _conn()
    try:
        _interaction(conn, "missing-task-vector")
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    task_embeddings = report["interaction_task_embeddings"]
    assert task_embeddings["interactions"] == 1
    assert task_embeddings["missing"] == 1
    assert task_embeddings["refresh_required"] == 1
    assert "interaction task embeddings: compatible=0/1" in (
        learning_health.format_report(report)
    )


def test_learning_report_tracks_grounding_signals_sources_and_hygiene():
    conn = _conn()
    try:
        for interaction_id in ("i1", "i2", "i3"):
            _interaction(conn, interaction_id)
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0)
        memory_store.record_outcome_row(conn, "i2", "accepted", 0.8)
        memory_store.record_outcome_row(conn, "i3", "failed", -1.0)
        memory_store.add_lesson(
            conn,
            "lesson-grounded",
            "Use a bounded retry for transient failures.",
            learning_health.embeddings.to_blob([1.0]),
            "i1",
        )
        memory_store.add_lesson(
            conn,
            "lesson-seed",
            "Validate generated manifests before packaging.",
            learning_health.embeddings.to_blob([1.0]),
            "seed:artifact:manifest",
        )
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["status"] == "watch"
    assert report["interactions"] == 3
    assert report["outcomes"] == 3
    assert report["outcome_interactions"] == 3
    assert report["good_outcomes"] == 2
    assert report["bad_outcomes"] == 1
    assert report["outcome_coverage_percent"] == 100.0
    assert report["positive_percent"] == 66.7
    assert report["grounded_lessons"] == 1
    assert report["synthetic_lessons"] == 1
    assert report["lesson_sources"] == {"interaction": 1, "seed": 1}
    assert report["distillation_yield"] == 0.5
    assert report["quality"]["embedding_percent"] == 100.0
    assert report["quality"]["embedding_legacy"] == 2
    assert report["quality"]["embedding_dimensions"] == {"1": 2}
    assert [row["signal"] for row in report["signals"]] == [
        "accepted",
        "failed",
        "tests_passed",
    ]


def _healthy_store(monkeypatch, judged=0):
    """A store with nothing wrong with it except how much of its work anyone has
    judged -- so the reviewed sample size is the only variable in these tests.

    `judged` caller-accepted outcomes, one per interaction, each interaction
    carrying a lesson grounded on it, so coverage and distillation yield stay
    perfect however many are asked for. Outcomes are unique per
    (interaction, signal), so a judged sample needs that many interactions.
    Caller closes the connection.
    """
    monkeypatch.setattr(learning_health.embeddings, "EXPECTED_DIMENSION", 1)
    conn = _conn()
    for index in range(max(1, judged)):
        interaction_id = "i%d" % index
        _interaction(conn, interaction_id)
        memory_store.refresh_interaction_task_embedding(
            conn,
            interaction_id,
            learning_health.embeddings.to_blob([1.0]),
            learning_health.embeddings.EMBED_IDENTITY,
            revision=learning_health.embeddings.EMBED_REVISION,
            dimension=1,
        )
        memory_store.add_lesson(
            conn,
            "lesson-%d" % index,
            "Pin the compiler before configuring CMake (case %d)." % index,
            b"\x01\x02\x03\x04",
            interaction_id,
            embedding_model=learning_health.embeddings.EMBED_IDENTITY,
            embedding_revision=learning_health.embeddings.EMBED_REVISION,
            embedding_dim=1,
        )
        if index < judged:
            memory_store.record_outcome_row(conn, interaction_id, "accepted", 0.8)
    return conn


def test_clean_grounded_learning_store_is_healthy_and_formats(monkeypatch):
    # A measurable caller-judged sample: below _MIN_REVIEWED_SAMPLE the gate has
    # nothing to believe and no store may read healthy, however clean it is.
    conn = _healthy_store(monkeypatch, judged=learning_health._MIN_REVIEWED_SAMPLE)
    try:
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    text = learning_health.format_report(report)
    assert report["status"] == "healthy"
    assert report["distillation_yield"] == 1.0
    assert "sonder learning health" in text
    assert "outcome coverage: 100.0%" in text
    assert "interaction=20" in text
    assert "accepted=20" in text
    assert "legacy=0" in text


def test_learning_health_flags_mixed_or_wrong_model_embeddings():
    conn = _conn()
    try:
        memory_store.add_lesson(
            conn, "current", "Current model lesson.",
            learning_health.embeddings.to_blob([1.0, 0.0]), "seed",
            embedding_model=learning_health.embeddings.EMBED_IDENTITY,
            embedding_revision=learning_health.embeddings.EMBED_REVISION,
            embedding_dim=2,
        )
        memory_store.add_lesson(
            conn, "stale", "Stale model lesson.",
            learning_health.embeddings.to_blob([1.0, 0.0, 0.0]), "seed",
            embedding_model="old-embed-model", embedding_dim=3,
        )
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["status"] == "attention"
    assert report["quality"]["embedding_model_mismatch"] == 1
    assert report["quality"]["embedding_mixed_dimensions"] is True
    assert report["quality"]["embedding_dimensions"] == {"2": 1, "3": 1}


def test_learning_health_checks_blob_shape_finiteness_and_actual_dimensions(
    monkeypatch,
):
    monkeypatch.setattr(learning_health.embeddings, "EMBED_REVISION", "rev-current")
    monkeypatch.setattr(learning_health.embeddings, "EXPECTED_DIMENSION", 2)
    conn = _conn()
    try:
        for lesson_id, vector, revision in (
            ("current", [1.0, 0.0], "rev-current"),
            ("metadata-mismatch", [1.0, 0.0, 0.0], "rev-old"),
            ("malformed", [1.0, 0.0], "rev-current"),
            ("nonfinite", [1.0, 0.0], "rev-current"),
        ):
            memory_store.add_lesson(
                conn,
                lesson_id,
                "Embedding integrity lesson %s." % lesson_id,
                learning_health.embeddings.to_blob(vector),
                "seed:health:test",
                embedding_model=learning_health.embeddings.EMBED_IDENTITY,
                embedding_revision=revision,
                embedding_dim=len(vector),
            )
        conn.execute(
            "UPDATE lessons SET embedding_dim=2 WHERE id='metadata-mismatch'"
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='malformed'",
            (b"\x00" * 6,),
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='nonfinite'",
            (learning_health.embeddings.to_blob([float("nan"), 0.0]),),
        )
        conn.commit()
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    quality = report["quality"]
    text = learning_health.format_report(report)
    assert report["status"] == "attention"
    assert quality["embedding_percent"] == 50.0
    assert quality["embedding_revision_mismatch"] == 1
    assert quality["embedding_dimension_invalid"] == 1
    assert quality["embedding_dimension_mismatch"] == 1
    assert quality["embedding_vector_invalid"] == 1
    assert quality["embedding_mixed_dimensions"] is True
    assert quality["embedding_dimensions"] == {"2": 2, "3": 1}
    assert "revision mismatch=1" in text
    assert "dimension invalid=1" in text
    assert "dimension mismatch=1" in text
    assert "invalid vectors=1" in text
    assert "mixed=yes" in text
    assert "target dimension=2" in text


def test_learning_health_flags_zero_norm_vector_for_refresh(monkeypatch):
    monkeypatch.setattr(learning_health.embeddings, "EXPECTED_DIMENSION", 2)
    conn = _conn()
    try:
        memory_store.add_lesson(
            conn, "zero", "Zero vectors are not semantic evidence.",
            learning_health.embeddings.to_blob([1.0, 0.0]), "seed",
            embedding_model=learning_health.embeddings.EMBED_IDENTITY,
            embedding_revision=learning_health.embeddings.EMBED_REVISION,
            embedding_dim=2,
        )
        conn.execute(
            "UPDATE lessons SET embedding=? WHERE id='zero'",
            (learning_health.embeddings.to_blob([0.0, 0.0]),),
        )
        conn.commit()
        report = learning_health.build_report(conn)
        selected = memory_store.lessons_needing_embedding_refresh(
            conn, learning_health.embeddings.EMBED_IDENTITY,
            revision=learning_health.embeddings.EMBED_REVISION,
            dimension=2,
        )
    finally:
        conn.close()

    assert report["status"] == "attention"
    assert report["quality"]["embedding_vector_invalid"] == 1
    assert report["quality"]["embedding_percent"] == 0.0
    assert [row["id"] for row in selected] == ["zero"]


def test_learning_health_reports_quarantined_lessons_as_watch():
    conn = _conn()
    try:
        memory_store.add_lesson(
            conn,
            "harmful",
            "Repeatedly harmful parser advice.",
            b"\x01\x02\x03\x04",
            "seed:quality:test",
        )
        for index in range(6):
            interaction_id = "harmful-use-%s" % index
            memory_store.log_lesson_usage(
                conn, ["harmful"], interaction_id, "parser task",
            )
            memory_store.record_lesson_usage_outcome(
                conn, interaction_id, "failed", -1.0,
            )
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    text = learning_health.format_report(report)
    assert report["status"] == "watch"
    assert report["evaluated_lessons"] == 1
    assert report["lessons_with_losses"] == 1
    assert report["loss_only_lessons"] == 1
    assert report["quarantined_lessons"] == 1
    assert report["quarantined_lesson_details"][0]["lesson_id"] == "harmful"
    assert report["quarantined_lesson_details"][0]["losses_since_win"] == 6
    assert report["quarantined_lesson_details"][0]["retry_after"]
    # Quarantine has no evidence-driven exit. A quarantined lesson is excluded
    # from retrieval, so the win that would clear it is unreachable until the
    # cooldown elapses; the report must not imply the store keeps re-testing it.
    assert "not on evidence" in report["quarantine_review"]
    assert "excluded from retrieval" in report["quarantine_review"]
    assert "quarantined=1" in text
    assert "quarantine harmful: losses=6" in text
    assert "not on evidence" in text


def test_autograded_curriculum_outcomes_do_not_mask_the_reviewed_hit_rate():
    """A blended positive_percent reads like "how often the model is right on my
    task", but is dominated by curriculum/ladder runs the runtime both sets and
    marks. Here 9 self-graded passes sit next to 1 accepted and 3 rejected: the
    blend says 77% while the model actually satisfied a caller 1 time in 4."""
    conn = _conn()
    try:
        ids = []
        for n in range(13):
            interaction_id = "i%d" % n
            _interaction(conn, interaction_id)
            ids.append(interaction_id)
        for interaction_id in ids[:9]:
            memory_store.record_outcome_row(conn, interaction_id, "tests_passed", 1.0)
        memory_store.record_outcome_row(conn, ids[9], "accepted", 0.8)
        for interaction_id in ids[10:]:
            memory_store.record_outcome_row(conn, interaction_id, "rejected", -0.5)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["outcomes"] == 13
    assert report["positive_percent"] == 76.9

    # The two populations are reported apart, so the flattering blend cannot
    # hide that reviewed work succeeded only a quarter of the time.
    assert report["autograded_outcomes"] == 9
    assert report["autograded_positive_percent"] == 100.0
    assert report["reviewed_outcomes"] == 4
    assert report["reviewed_positive_percent"] == 25.0

    text = learning_health.format_report(report)
    assert "reviewed (judged by a caller): 4 | positive: 25.0%" in text
    assert "autograded (runtime marking its own curriculum): 9 | positive: 100.0%" in text


def _graded(conn, interaction_id, lesson_ids, task, reward):
    memory_store.log_lesson_usage(conn, lesson_ids, interaction_id, task)
    memory_store.record_lesson_usage_outcome(
        conn, interaction_id, "tests_passed" if reward > 0 else "failed", reward,
    )


def test_status_gates_on_the_caller_judged_rate_not_the_blend():
    """The blend was fixed in the *display* and left in the *gate*. The live
    store reported positive 96.1% while caller-judged work sat at 52.7% over 186
    reviewed outcomes, and _status compared 96.1 against its 60/80 thresholds --
    so a store failing half the work a caller delegated read as "watch". The
    thresholds now apply to the reviewed rate once the sample can carry them."""
    conn = _conn()
    try:
        for n in range(500):
            memory_store.record_outcome_row(conn, "auto%d" % n, "tests_passed", 1.0)
        for n in range(30):
            memory_store.record_outcome_row(conn, "ok%d" % n, "accepted", 0.8)
        for n in range(30):
            memory_store.record_outcome_row(conn, "no%d" % n, "rejected", -0.5)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["reviewed_outcomes"] == 60
    assert report["reviewed_positive_percent"] == 50.0
    # The blend clears both thresholds on its own; only the reviewed rate does not.
    assert report["positive_percent"] == 94.6
    assert report["status"] == "attention"
    assert "blended, not an accuracy figure" in learning_health.format_report(report)


def test_a_reviewed_sample_too_small_to_gate_on_is_unmeasured_not_blended():
    """Four judgements cannot carry a 60%/80% threshold -- but the blend is not
    a weaker version of that number, it is a different population, so falling
    back to it answers a question nobody asked. Below _MIN_REVIEWED_SAMPLE the
    gate has nothing to believe and says so; the split stays visible in the
    text, which is where the honest number lives.

    Flipping to "attention" on a single rejection would still make the status
    meaningless, so unmeasured is not treated as measured-bad: it is its own
    state, and it costs the store "healthy", not a red flag."""
    conn = _conn()
    try:
        for n in range(9):
            memory_store.record_outcome_row(conn, "auto%d" % n, "tests_passed", 1.0)
        memory_store.record_outcome_row(conn, "ok", "accepted", 0.8)
        for n in range(3):
            memory_store.record_outcome_row(conn, "no%d" % n, "rejected", -0.5)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["reviewed_outcomes"] == 4
    assert report["reviewed_positive_percent"] == 25.0
    assert report["status"] != "attention"
    assert learning_health._gating_positive_percent(report) == (None, "unmeasured")


def test_an_unmeasured_reviewed_sample_cannot_read_healthy(monkeypatch):
    """The B2 defect, whole: a store with a large self-graded curriculum and no
    caller judgements at all blended to ~100% positive, cleared both thresholds
    and reported "healthy" -- a green check in the UI for a store that has never
    once been judged. calibration.should_verify fails closed on exactly this
    ignorance at exactly this threshold; the status gate now agrees."""
    conn = _healthy_store(monkeypatch, judged=0)
    try:
        for n in range(500):
            memory_store.record_outcome_row(conn, "auto%d" % n, "tests_passed", 1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["reviewed_outcomes"] == 0
    assert report["positive_percent"] > 99.0, "the blend alone clears every bar"
    assert report["status"] == "watch"
    assert "reviewed sample too small to gate on" in learning_health.format_report(report)


def test_a_thin_reviewed_sample_costs_a_clean_store_its_healthy_verdict(monkeypatch):
    """One judgement short of the sample is still ignorance. 19 perfect
    outcomes are not a track record, exactly as 5 are not in calibration."""
    conn = _healthy_store(monkeypatch, judged=learning_health._MIN_REVIEWED_SAMPLE - 1)
    try:
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["reviewed_outcomes"] == learning_health._MIN_REVIEWED_SAMPLE - 1
    assert report["reviewed_positive_percent"] == 100.0
    assert report["status"] == "watch"


def test_a_measurable_reviewed_sample_can_still_read_healthy(monkeypatch):
    """Fail-closed must not mean permanently pessimistic: once the sample can
    carry the thresholds, a good record reads healthy again."""
    conn = _healthy_store(monkeypatch, judged=learning_health._MIN_REVIEWED_SAMPLE)
    try:
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["reviewed_outcomes"] == learning_health._MIN_REVIEWED_SAMPLE
    assert learning_health._gating_positive_percent(report) == (100.0, "reviewed")
    assert report["status"] == "healthy"


def test_loss_only_is_measured_against_its_own_reference_class():
    """Losing is correlational. On the live store the loss rate collapses with
    retrieval count -- 50.6% for lessons retrieved once, 1.2% for lessons
    retrieved 100+ times -- because retrieval is similarity-driven, so a rare
    lesson is the one pulled in for an unusual task, and unusual tasks fail.
    Reporting loss-only=52 against an implicit zero (or against the corpus-wide
    5.3%) reads as 52 bad lessons; 47.1 of them are what task difficulty alone
    predicts. This fixture reproduces the gradient in miniature: ten single-
    retrieval losers, all of them ordinary for their band."""
    conn = _conn()
    try:
        memory_store.add_lesson(conn, "popular", "Popular lesson.", None, "seed")
        for n in range(40):
            _graded(conn, "pop%d" % n, ["popular"], "routine task %d" % n, 1.0)
        for n in range(10):
            memory_store.add_lesson(
                conn, "rare-win%d" % n, "Rare win %d." % n, None, "seed",
            )
            _graded(conn, "rw%d" % n, ["rare-win%d" % n], "odd task w%d" % n, 1.0)
        for n in range(10):
            memory_store.add_lesson(
                conn, "rare-loss%d" % n, "Rare loss %d." % n, None, "seed",
            )
            _graded(conn, "rl%d" % n, ["rare-loss%d" % n], "odd task l%d" % n, -1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["loss_only_lessons"] == 10
    assert report["loss_only_single_retrieval_lessons"] == 10
    # Corpus-wide the store lost 10 of 60 scored retrievals (16.7%), which would
    # predict fewer than 4 loss-only lessons. Their own band lost half of its
    # retrievals, and predicts all 10.
    assert report["scored_retrieval_loss_percent"] == 16.7
    assert 9.0 <= report["loss_only_expected_from_task_difficulty"] <= 11.0
    assert report["loss_only_beyond_reference_class"] == 0
    assert "not per-lesson evidence" in report["loss_attribution_note"]
    assert "expected from task difficulty alone" in learning_health.format_report(report)


def test_a_lesson_that_keeps_losing_alone_still_stands_out():
    """The reference class must not excuse everything, or the metric is inert.
    A lesson that lost every one of six retrievals in a band that mostly wins is
    a 1-in-a-million event for that band, and is reported as beyond it -- the
    five live lessons with four-plus consecutive losses are the only part of
    loss-only=52 the data can call suspect."""
    conn = _conn()
    try:
        for n in range(10):
            memory_store.add_lesson(
                conn, "ok%d" % n, "Fine lesson %d." % n, None, "seed",
            )
            for use in range(6):
                _graded(
                    conn, "ok%d-%d" % (n, use), ["ok%d" % n],
                    "task %d %d" % (n, use), 1.0,
                )
        memory_store.add_lesson(conn, "bad", "Consistently harmful advice.", None, "seed")
        for use in range(6):
            _graded(conn, "bad-%d" % use, ["bad"], "distinct task %d" % use, -1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["loss_only_lessons"] == 1
    assert report["loss_only_beyond_reference_class"] == 1
    assert report["loss_only_expected_from_task_difficulty"] < 1.0


def test_one_failure_cluster_no_longer_quarantines_its_whole_cohort():
    """record_lesson_usage_outcome writes a task's reward onto every lesson
    retrieved for it, so co-retrieved lessons share a verdict. Four of the six
    lessons quarantined on the live store crossed the five-loss threshold on the
    identical five interactions: one cluster counted four times as independent
    evidence.

    Blame is now split across the cohort, so five failures shared by four
    lessons give each 1.25 attributable losses -- under the 2.0 threshold, and
    none is quarantined. The shared-blame counters still report the cluster, so
    the evidence stays visible without being multiplied."""
    conn = _conn()
    try:
        cohort = ["co%d" % n for n in range(4)]
        for lesson_id in cohort:
            memory_store.add_lesson(
                conn, lesson_id, "Advice %s." % lesson_id, None, "seed",
            )
        for n in range(5):
            _graded(conn, "shared-failure-%d" % n, cohort, "distinct task %d" % n, -1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    # Five failures shared four ways is 1.25 attributable losses each.
    assert report["quarantined_lessons"] == 0
    assert report["failed_interactions"] == 5
    assert report["lessons_marked_per_failed_interaction"] == 4.0
    # The cluster is still reported, just not counted four times.
    assert report["lessons_with_losses"] == 4


def test_a_lesson_failing_alone_is_still_quarantined():
    """Splitting blame must not make the criterion toothless. A lesson that is
    the ONLY one retrieved for each of its failures owns that evidence
    outright, so its attributable losses equal its raw losses and it still
    crosses the threshold -- unlike the shared cluster above, which does not.

    This is the distinction the raw-count criterion could not draw: on the live
    store every one of the six quarantined lessons had a peer covering 100% of
    its failing interactions, while 18 failing interactions blame exactly one
    lesson."""
    conn = _conn()
    try:
        memory_store.add_lesson(conn, "solo", "Advice solo.", None, "seed")
        for n in range(5):
            _graded(conn, "solo-failure-%d" % n, ["solo"], "distinct task %d" % n, -1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["quarantined_lessons"] == 1
    assert report["lessons_marked_per_failed_interaction"] == 1.0


def test_a_lesson_whose_interaction_is_gone_is_orphaned_not_synthetic():
    """_lesson_source bucketed an unresolvable source by ``split(":")[0]``, so a
    lesson naming a pruned interaction became its own lesson_sources bucket and
    was counted as synthetic. It is the opposite: it was earned from a real
    outcome and then lost the evidence, and 533 of the live store's 1061 lessons
    are one interaction-prune away from being miscounted that way."""
    conn = _conn()
    try:
        memory_store.add_lesson(
            conn, "orphan", "Earned but unbacked.", None, "0123456789abcdef",
        )
        memory_store.add_lesson(conn, "seeded", "Seeded text.", None, "seed:algo")
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["lesson_sources"] == {"orphaned": 1, "seed": 1}
    assert report["orphaned_lessons"] == 1
    assert report["synthetic_lessons"] == 1
    assert report["grounded_lessons"] == 0
    assert "orphaned: 1" in learning_health.format_report(report)


def test_never_validated_lessons_are_surfaced_next_to_the_synthetic_count():
    """Synthetic is not the whole unvalidated population: a grounded lesson that
    has never been retrieved on a scored task has also never been checked
    against reality. The live store is 528 synthetic but 715 never validated, so
    reading only the synthetic count understates the unproven corpus by 187
    lessons."""
    conn = _conn()
    try:
        _interaction(conn, "i1")
        memory_store.add_lesson(
            conn, "earned-untested", "Earned, never retrieved.", None, "i1",
        )
        memory_store.add_lesson(conn, "seeded", "Seeded, never retrieved.", None, "seed")
        memory_store.add_lesson(conn, "proven", "Proven in production.", None, "seed")
        _graded(conn, "u1", ["proven"], "task", 1.0)
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["grounded_lessons"] == 1
    assert report["synthetic_lessons"] == 2
    assert report["unvalidated_lessons"] == 2
    assert report["synthetic_unvalidated_lessons"] == 1
    assert "never validated by an outcome: 2 (synthetic: 1)" in (
        learning_health.format_report(report)
    )


def _distillation_row(conn, interaction_id, state, reason):
    conn.execute(
        "INSERT INTO lesson_distillations(interaction_id, state, result_reason) "
        "VALUES(?, ?, ?)",
        (interaction_id, state, reason),
    )
    conn.commit()


def test_distillation_reason_breakdown_says_where_yield_was_lost():
    """distillation_yield reported how much yield survived and nothing reported
    where the rest went, so answering "which gate refuses candidates?" meant
    replaying 80 historical interactions through a live model -- the database
    could not answer it. That replay is what caught the semantic dedup gate
    being dead in production, a fault that reads straight off this breakdown as
    zero semantic_duplicate rows corpus-wide.

    The breakdown must therefore reach the report and the rendered view, and it
    must group by state as well as reason: 'stored' and 'no_lesson' are
    different outcomes and a bare reason column cannot tell them apart."""
    conn = _conn()
    try:
        _distillation_row(conn, "d1", "no_lesson", "semantic_duplicate")
        _distillation_row(conn, "d2", "no_lesson", "semantic_duplicate")
        _distillation_row(conn, "d3", "no_lesson", "exact_duplicate")
        _distillation_row(conn, "d4", "stored", "stored")
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["distillation_reasons"] == [
        {"state": "no_lesson", "reason": "semantic_duplicate", "count": 2},
        {"state": "no_lesson", "reason": "exact_duplicate", "count": 1},
        {"state": "stored", "reason": "stored", "count": 1},
    ]
    assert report["distillation_reason_rows"] == 4
    assert report["distillation_reason_recorded"] == 4
    assert report["distillation_reason_unrecorded"] == 0
    assert report["distillation_reason_recorded_percent"] == 100.0
    rendered = learning_health.format_report(report)
    assert "distillation reasons: 4 of 4 terminal row(s) carry one (100.0%)" in rendered
    assert "recorded: no_lesson/semantic_duplicate=2" in rendered


def test_rows_predating_the_reason_column_are_a_bucket_not_a_zero():
    """result_reason is NULL on every row written before the column existed,
    and that is 6712 of the live ledger's 6981 terminal rows (2026-08-07) --
    96.1%. Reporting those as zero, or naming them, would turn "we never
    recorded this" into a confident wrong answer about which gate fired, which
    is exactly the mistake this breakdown exists to prevent.

    So: the unrecorded rows keep reason=None in the structured report (never an
    invented string), they are counted in their own bucket, and the rendered
    view lists them apart from the recorded reasons so '(not recorded)' cannot
    read as one more rejection reason with a name."""
    conn = _conn()
    try:
        _distillation_row(conn, "legacy1", "legacy_no_lesson", None)
        _distillation_row(conn, "legacy2", "legacy_no_lesson", None)
        _distillation_row(conn, "legacy3", "stored", None)
        _distillation_row(conn, "cancelled1", "cancelled", None)
        _distillation_row(conn, "recent", "no_lesson", "not_concrete")
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert {"state": "legacy_no_lesson", "reason": None, "count": 2} in (
        report["distillation_reasons"]
    )
    assert report["distillation_reason_rows"] == 5
    assert report["distillation_reason_recorded"] == 1
    assert report["distillation_reason_unrecorded"] == 4
    assert report["distillation_reason_recorded_percent"] == 20.0
    # No row anywhere in the structured breakdown invents a name for NULL.
    assert not [
        row
        for row in report["distillation_reasons"]
        if row["reason"] == learning_health._UNRECORDED_REASON_LABEL
    ]
    rendered = learning_health.format_report(report)
    assert "distillation reasons: 1 of 5 terminal row(s) carry one (20.0%)" in rendered
    assert "recorded: no_lesson/not_concrete=1" in rendered
    assert "(not recorded): legacy_no_lesson=2, cancelled=1, stored=1" in rendered
    assert "unknown, not unrefused" in rendered


def test_in_flight_claims_are_not_counted_as_missing_reasons():
    """memory_store.distillation_reason_counts applies no state filter by
    default, so a claimed or retryable row -- which cannot have a reason yet,
    because nothing has decided its outcome -- would be counted alongside rows
    that finished without one. That would make the recorded-coverage figure
    move with queue depth instead of with recording, and would report an
    in-flight interaction as a distillation whose reason went unrecorded.

    Only the four terminal states are counted."""
    conn = _conn()
    try:
        _distillation_row(conn, "done", "no_lesson", "not_concrete")
        _distillation_row(conn, "retrying", "retryable", None)
        conn.execute(
            "INSERT INTO lesson_distillations(interaction_id, state, "
            "claim_token, owner_pid, owner_identity, claimed_at) "
            "VALUES('inflight', 'claimed', 'tok', 1, 'owner', 1.0)"
        )
        conn.commit()
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    assert report["distillation_reason_rows"] == 1
    assert report["distillation_reason_unrecorded"] == 0
    assert report["distillation_reason_recorded_percent"] == 100.0
    assert [row["state"] for row in report["distillation_reasons"]] == ["no_lesson"]


def test_caller_judged_outcomes_are_attributed_to_the_tier_that_produced_them():
    """The aggregate reviewed rate does not say which tier is being rejected.

    Also pins the orphan rule: an outcome whose interaction row is gone stays
    in the breakdown under its own label. An inner join would drop it, and a
    per-tier table that does not add up to reviewed_outcomes is the same
    "count is really a floor" defect this module already carries scars from.
    """
    conn = _conn()
    try:
        for interaction_id, tier in (
            ("c1", "code"), ("c2", "code"), ("c3", "code"),
            ("g1", "cloud-code"),
        ):
            memory_store.log_interaction(
                conn, interaction_id, "task", "", "answer", tier
            )
        memory_store.record_outcome_row(conn, "c1", "accepted", 0.8)
        memory_store.record_outcome_row(conn, "c2", "rejected", -0.5)
        memory_store.record_outcome_row(conn, "c3", "rejected", -0.5)
        memory_store.record_outcome_row(conn, "g1", "accepted", 0.8)
        # No interaction row was ever logged for this one.
        memory_store.record_outcome_row(conn, "vanished", "accepted", 0.8)
        # Autograded: must not appear in a caller-judged breakdown at all.
        memory_store.record_outcome_row(conn, "c1", "tests_passed", 1.0)

        report = learning_health.build_report(conn)
        rendered = learning_health.format_report(report)
    finally:
        conn.close()

    # The invariant that makes the table trustworthy, asserted first so a
    # regression reports the shortfall rather than a KeyError on whichever
    # tier happened to go missing.
    assert sum(row["outcomes"] for row in report["reviewed_by_tier"]) == report[
        "reviewed_outcomes"
    ]

    by_tier = {row["tier"]: row for row in report["reviewed_by_tier"]}
    assert by_tier["code"]["outcomes"] == 3
    assert by_tier["code"]["positive_percent"] == 33.3
    assert by_tier["cloud-code"]["outcomes"] == 1
    assert by_tier["(unattributed)"]["outcomes"] == 1
    assert "by tier" in rendered
    assert "small sample" in rendered


def test_failed_blame_attribution_is_reported_not_swallowed(monkeypatch):
    """A bare except downgraded the loss stats to the undiscounted upper bound
    and said nothing, so the report claimed lessons were quarantined that
    retrieval still served -- a monitoring surface disagreeing with the
    behaviour it exists to monitor.

    The trigger is ordinary: attribution runs two full-table scans, so any
    concurrent writer can raise "database is locked" here.
    """
    import retriever

    conn = _conn()
    try:
        for interaction_id in ("i1", "i2"):
            _interaction(conn, interaction_id)
        memory_store.record_outcome_row(conn, "i1", "accepted", 0.8)
        memory_store.record_outcome_row(conn, "i2", "rejected", -0.5)

        clean = learning_health.build_report(conn)
        assert clean["lesson_attribution_error"] == ""
        assert "UPPER BOUND" not in learning_health.format_report(clean)

        def boom(_conn):
            raise Exception("database is locked")

        monkeypatch.setattr(retriever, "usage_stats_with_attribution", boom)
        degraded = learning_health.build_report(conn)
    finally:
        conn.close()

    assert "database is locked" in degraded["lesson_attribution_error"]
    rendered = learning_health.format_report(degraded)
    feedback = [ln for ln in rendered.splitlines() if "lesson feedback" in ln]
    assert feedback, "the lesson feedback line should still render"
    # The caveat rides on the line carrying the numbers it invalidates, not in
    # a footnote a reader quoting the quarantine count would skip.
    assert "UPPER BOUND" in feedback[0]
