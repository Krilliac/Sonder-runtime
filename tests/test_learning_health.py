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
            b"\x00\x00\x00\x00",
            "i1",
        )
        memory_store.add_lesson(
            conn,
            "lesson-seed",
            "Validate generated manifests before packaging.",
            b"\x00\x00\x00\x00",
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
    assert [row["signal"] for row in report["signals"]] == [
        "accepted",
        "failed",
        "tests_passed",
    ]


def test_clean_grounded_learning_store_is_healthy_and_formats():
    conn = _conn()
    try:
        _interaction(conn, "i1")
        memory_store.record_outcome_row(conn, "i1", "compiled", 0.7)
        memory_store.add_lesson(
            conn,
            "lesson-one",
            "Pin the compiler before configuring CMake.",
            b"\x01\x02\x03\x04",
            "i1",
        )
        report = learning_health.build_report(conn)
    finally:
        conn.close()

    text = learning_health.format_report(report)
    assert report["status"] == "healthy"
    assert report["distillation_yield"] == 1.0
    assert "trilobite learning health" in text
    assert "outcome coverage: 100.0%" in text
    assert "interaction=1" in text
    assert "compiled=1" in text
