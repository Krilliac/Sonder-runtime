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

    plan, deleted = memory_quality.repair_exact_duplicates(conn, apply=True)
    assert deleted == 1
    assert len(memory_store.all_lessons(conn)) == 1


def test_audit_ignores_non_interaction_sources_for_missing_source():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "seeded", "Use bisect for sorted inserts.", None, "seed:algo")
    memory_store.add_lesson(conn, "community", "Use deque for queues.", None, "community")

    report = memory_quality.audit(conn)

    assert report["missing_source_interaction"] == 0
