import memory_quality
import memory_store


def test_exact_duplicate_plan_keeps_best_scored_lesson():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "old", "Use pathlib.Path for joins.", None, "a1")
    memory_store.add_lesson(conn, "winner", " use pathlib.path for joins. ", None, "a2")
    memory_store.add_lesson(conn, "other", "Use collections.Counter for counts.", None, "a3")
    memory_store.log_lesson_usage(conn, ["winner"], "i1", "task")
    memory_store.record_lesson_usage_outcome(conn, "i1", "tests_passed", 1.0)

    plan = memory_quality.exact_duplicate_plan(conn)

    assert len(plan) == 1
    assert plan[0]["keeper_id"] == "winner"
    assert plan[0]["prune_ids"] == ["old"]


def test_repair_exact_duplicates_dry_run_and_apply():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "a", "Prefer early returns.", None, "seed")
    memory_store.add_lesson(conn, "b", "Prefer early returns.", None, "seed")

    plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=False)
    assert deleted == 0
    assert len(memory_store.all_lessons(conn)) == 2

    plan, deleted = memory_quality.repair_exact_duplicates(
        conn, apply="false",
    )
    assert deleted == 0
    assert len(memory_store.all_lessons(conn)) == 2

    plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=True)
    assert deleted == 1
    assert len(memory_store.all_lessons(conn)) == 1


def test_audit_ignores_non_interaction_sources_for_missing_source():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "seeded", "Use bisect for sorted inserts.", None, "seed:algo")
    memory_store.add_lesson(conn, "community", "Use deque for queues.", None, "community")

    report = memory_quality.audit(conn)

    assert report["missing_source_interaction"] == 0


def test_privacy_findings_are_redacted_and_cleanup_requires_explicit_flagged_ids():
    conn = memory_store.connect(":memory:")
    private_text = "Use C:\\Users\\alice\\private\\notes.txt and token=hidden-value"
    memory_store.add_lesson(conn, "private", private_text, None, "seed")
    memory_store.add_lesson(conn, "safe", "Use pathlib.Path for joins.", None, "seed")

    findings = memory_quality.privacy_findings(conn)
    plan = memory_quality.privacy_cleanup_plan(conn, ["private", "safe", "missing"])
    report = memory_quality.format_audit(memory_quality.audit(conn), sample_limit=5)

    assert [row["id"] for row in findings] == ["private"]
    assert private_text not in repr(findings)
    assert "hidden-value" not in report
    assert [row["id"] for row in plan["eligible"]] == ["private"]
    assert plan["not_flagged"] == ["safe"]
    assert plan["missing"] == ["missing"]

    deleted = memory_quality.apply_privacy_cleanup(conn, plan)
    assert deleted == 1
    assert memory_store.get_lesson_text(conn, "private") is None
    assert memory_store.get_lesson_text(conn, "safe") is not None
