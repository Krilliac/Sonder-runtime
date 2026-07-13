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


def test_clean_grounded_learning_store_is_healthy_and_formats(monkeypatch):
    monkeypatch.setattr(learning_health.embeddings, "EXPECTED_DIMENSION", 1)
    conn = _conn()
    try:
        _interaction(conn, "i1")
        memory_store.refresh_interaction_task_embedding(
            conn,
            "i1",
            learning_health.embeddings.to_blob([1.0]),
            learning_health.embeddings.EMBED_IDENTITY,
            revision=learning_health.embeddings.EMBED_REVISION,
            dimension=1,
        )
        memory_store.record_outcome_row(conn, "i1", "compiled", 0.7)
        memory_store.add_lesson(
            conn,
            "lesson-one",
            "Pin the compiler before configuring CMake.",
            b"\x01\x02\x03\x04",
            "i1",
            embedding_model=learning_health.embeddings.EMBED_IDENTITY,
            embedding_revision=learning_health.embeddings.EMBED_REVISION,
            embedding_dim=1,
        )
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    text = learning_health.format_report(report)
    assert report["status"] == "healthy"
    assert report["distillation_yield"] == 1.0
    assert "sonder learning health" in text
    assert "outcome coverage: 100.0%" in text
    assert "interaction=1" in text
    assert "compiled=1" in text
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
    assert "automatically re-enter probation" in report["quarantine_review"]
    assert "quarantined=1" in text
    assert "quarantine harmful: losses=6" in text
