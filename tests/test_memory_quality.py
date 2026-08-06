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


def test_measured_zero_reward_outranks_a_measured_negative_reward():
    """The keeper rule read the average as ``avg_reward or -2.0``, so a lesson
    the store had actually graded 0.0 collapsed onto the never-evaluated rank
    and lost to a duplicate graded -1.0. Neutral evidence is not absent
    evidence, and it is certainly not worse than harmful evidence."""
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "neutral", "Prefer explicit imports.", None, "a1")
    memory_store.add_lesson(conn, "harmful", "Prefer explicit imports.", None, "a2")
    stats = {
        "neutral": {"avg_reward": 0.0, "wins": 0, "uses": 1},
        "harmful": {"avg_reward": -1.0, "wins": 0, "uses": 1},
    }
    group = [
        {"id": "neutral", "text": "Prefer explicit imports.", "ts": "2026-01-01"},
        {"id": "harmful", "text": "Prefer explicit imports.", "ts": "2026-01-01"},
    ]

    keeper = memory_quality.choose_exact_duplicate_keeper(group, stats)

    assert keeper["id"] == "neutral"
    conn.close()


def test_unevaluated_duplicate_never_launders_away_measured_harm():
    """Exact duplicates share their text, so the copy carrying outcome history
    is the one worth keeping even when that history is bad -- dropping it would
    delete the evidence (and any quarantine built on it) and leave a clean-
    looking row behind. "No evidence" must therefore stay ranked below every
    real reward, unlike the neutral 0.0 case above."""
    stats = {"harmful": {"avg_reward": -1.0, "wins": 0, "uses": 5}}
    group = [
        {"id": "unused", "text": "Retry on any exception.", "ts": "2026-01-01"},
        {"id": "harmful", "text": "Retry on any exception.", "ts": "2026-01-02"},
    ]

    assert memory_quality.choose_exact_duplicate_keeper(group, stats)["id"] == "harmful"


def test_audit_separates_ungrounded_and_never_validated_lessons():
    """Text hygiene cannot see provenance. On the live store 528 of 1061
    lessons were seeded and 715 had never been retrieved on a scored task, yet
    every hygiene counter read clean -- a seed lesson with a code anchor and a
    full stop looks perfect. The audit now says how much of the corpus its own
    counters are silent about."""
    conn = memory_store.connect(":memory:")
    memory_store.log_interaction(conn, "i1", "task", "", "answer", "code")
    memory_store.add_lesson(conn, "earned", "Pin `cl.exe` before configuring.", None, "i1")
    memory_store.add_lesson(conn, "seeded", "Use `bisect` for sorted inserts.", None, "seed:algo")
    memory_store.log_lesson_usage(conn, ["earned"], "i2", "task")
    memory_store.record_lesson_usage_outcome(conn, "i2", "tests_passed", 1.0)

    report = memory_quality.audit(conn)
    text = memory_quality.format_audit(report)

    assert report["grounded_lessons"] == 1
    assert report["synthetic_lessons"] == 1
    assert report["unvalidated_lessons"] == 1
    assert report["synthetic_unvalidated_lessons"] == 1
    assert report["vague_without_anchor"] == 0
    assert "never validated by an outcome: 1 (of which synthetic: 1)" in text
    conn.close()
