import memory_quality
import memory_store
from sonder_runtime.domain.memory import rules as memory_rules


def test_exact_duplicate_plan_keeps_best_scored_lesson():
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "old", "Use pathlib.Path for joins.", None, "a1")
    memory_store.add_lesson(conn, "winner", " use pathlib.path for joins. ", None, "a2")
    memory_store.add_lesson(conn, "other", "Use collections.Counter for counts.", None, "a3")
    memory_store.log_lesson_usage(conn, ["winner"], "i1", "task")
    memory_store.record_lesson_usage_outcome(conn, "i1", "tests_passed", 1.0, source="caller")

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
        "neutral": {"avg_reward_caller": 0.0, "wins": 0, "uses": 1},
        "harmful": {"avg_reward_caller": -1.0, "wins": 0, "uses": 1},
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
    stats = {"harmful": {"avg_reward_caller": -1.0, "wins": 0, "uses": 5}}
    group = [
        {"id": "unused", "text": "Retry on any exception.", "ts": "2026-01-01"},
        {"id": "harmful", "text": "Retry on any exception.", "ts": "2026-01-02"},
    ]

    assert memory_quality.choose_exact_duplicate_keeper(group, stats)["id"] == "harmful"


def test_a_self_graded_duplicate_never_outranks_a_caller_reviewed_one():
    """The surviving sibling of the fine-tuning corpus inversion.

    ``avg_reward`` is a mean over BOTH outcome populations, and the keeper was
    ranked on it -- so a lesson the runtime graded 1.0 by running its own tests
    beat one a caller reviewed and accepted at 0.8, and the reviewed copy was
    the row deleted. This is a real ordering, unlike the boolean EXISTS filters
    elsewhere in the store, so the same rule applies: never one mean over both.
    """
    stats = {
        "self": {
            "avg_reward": 1.0, "avg_reward_caller": None,
            "avg_reward_execution": 1.0, "wins": 1, "uses": 1,
        },
        "human": {
            "avg_reward": 0.8, "avg_reward_caller": 0.8,
            "avg_reward_execution": None, "wins": 1, "uses": 1,
        },
    }
    group = [
        {"id": "self", "text": "Prefer explicit imports.", "ts": "2026-01-01"},
        {"id": "human", "text": "Prefer explicit imports.", "ts": "2026-01-02"},
    ]

    assert memory_quality.choose_exact_duplicate_keeper(group, stats)["id"] == "human"


def test_lesson_usage_stats_reports_the_two_populations_apart():
    """The split has to come from the store, not be guessed downstream."""
    conn = memory_store.connect(":memory:")
    memory_store.add_lesson(conn, "lesson", "Prefer explicit imports.", None, "a1")
    memory_store.log_interaction(conn, "i1", "t1", "", "r1", "code")
    memory_store.log_interaction(conn, "i2", "t2", "", "r2", "code")
    conn.execute(
        "INSERT INTO lesson_usage(lesson_id,interaction_id,task,outcome_signal,reward)"
        " VALUES(?,?,?,?,?)", ("lesson", "i1", "t1", "tests_passed", 1.0),
    )
    conn.execute(
        "INSERT INTO lesson_usage(lesson_id,interaction_id,task,outcome_signal,reward)"
        " VALUES(?,?,?,?,?)", ("lesson", "i2", "t2", "rejected", -0.5),
    )
    conn.commit()

    row = memory_store.lesson_usage_stats(conn)["lesson"]

    assert row["avg_reward_execution"] == 1.0
    assert row["avg_reward_caller"] == -0.5
    # The blended mean is still reported, but it is nobody's ranking key.
    assert row["avg_reward"] == 0.25
    conn.close()


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
    memory_store.record_lesson_usage_outcome(conn, "i2", "tests_passed", 1.0, source="caller")

    report = memory_quality.audit(conn)
    text = memory_quality.format_audit(report)

    assert report["grounded_lessons"] == 1
    assert report["synthetic_lessons"] == 1
    assert report["unvalidated_lessons"] == 1
    assert report["synthetic_unvalidated_lessons"] == 1
    assert report["vague_without_anchor"] == 0
    assert "never validated by an outcome: 1 (of which synthetic: 1)" in text
    conn.close()


def test_orphan_fts_counter_is_not_capped_by_its_sample(tmp_path):
    """len() of a LIMIT-20 slice was published as the orphan counter, on the
    same output line as missing_fts, which is an uncapped full scan. A store
    with 5000 dangling FTS rows therefore reported "orphan=20" beside an honest
    number and read as a small, bounded problem."""
    import sqlite3

    import memory_quality
    import memory_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    memory_store.init_db(conn)
    try:
        for i in range(25):
            conn.execute(
                "INSERT INTO lessons_fts(lesson_id, text) VALUES (?,?)",
                ("ghost%d" % i, "orphaned"),
            )
        conn.commit()
        report = memory_quality.audit(conn)
        assert report["orphan_fts"] == 25, "the counter must not stop at the cap"
        assert report["orphan_fts_sampled"] == 20, "the sample stays bounded"
        assert len(report["samples"]["orphan_fts"]) == 5
    finally:
        conn.close()


def test_orphan_fts_is_zero_on_a_clean_store(tmp_path):
    import sqlite3

    import memory_quality
    import memory_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    memory_store.init_db(conn)
    try:
        assert memory_quality.audit(conn)["orphan_fts"] == 0
    finally:
        conn.close()


def test_retryable_backlog_is_not_capped_by_the_drain_window():
    """list_retryable_distillations returns a LIMIT-bounded window, so counting
    what stayed deferred inside it answers "how much of this batch failed", not
    "how big is the backlog". Draining 16 of 500 successfully reported
    "still deferred 0" with 484 outstanding."""
    import sqlite3

    import memory_store as ms

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ms.init_db(conn)
    try:
        for i in range(500):
            conn.execute(
                "INSERT INTO lesson_distillations("
                "interaction_id, state, signal, attempts, created_ts, updated_ts)"
                " VALUES (?,?,?,?,?,?)",
                ("i%d" % i, ms.DISTILLATION_RETRYABLE, "tests_passed", 1, 1, 1),
            )
        conn.commit()
        assert len(ms.list_retryable_distillations(conn, 16)) == 16
        assert ms.count_retryable_distillations(conn) == 500
    finally:
        conn.close()


def test_retryable_backlog_is_zero_on_a_clean_store():
    import sqlite3

    import memory_store as ms

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ms.init_db(conn)
    try:
        assert ms.count_retryable_distillations(conn) == 0
    finally:
        conn.close()


def test_the_keeper_rule_is_the_written_domain_rule_not_a_local_copy():
    """#27: this module and ``rules.evidence_rank`` taught contradictory rules
    for the same concept, under the same NAME.

    ``choose_exact_duplicate_keeper`` held a closure literally called
    ``evidence_rank`` that made the caller mean key 1 unconditionally, while
    ``rules.evidence_rank`` deliberately conditions population on eligibility.
    Both are right -- for DIFFERENT questions -- but nothing said so and
    nothing tested it, which is the B2 shape. The keeper now reads the domain
    rule for its question rather than keeping a second, silent copy.
    """
    stats = {
        "rejected": {"avg_reward_caller": -0.5, "avg_reward_execution": None,
                     "wins": 0, "uses": 1},
        "self_graded": {"avg_reward_caller": None, "avg_reward_execution": 1.0,
                        "wins": 1, "uses": 1},
    }
    group = [
        {"id": "rejected", "text": "Retry on any exception.", "ts": "2026-01-01"},
        {"id": "self_graded", "text": "Retry on any exception.", "ts": "2026-01-02"},
    ]

    keeper = memory_quality.choose_exact_duplicate_keeper(group, stats)

    # The row a caller judged is the one whose history would be lost.
    assert keeper["id"] == "rejected"
    # ... and the ordering is the one written down in the rules, not a local
    # restatement of it that can drift.
    assert memory_rules.retention_rank(-0.5, None) < memory_rules.retention_rank(None, 1.0)
    assert not hasattr(memory_quality, "_NO_EVIDENCE_RANK"), (
        "the never-measured rank belongs to the rules, in one place"
    )


def test_the_keeper_rule_is_not_the_credit_rule():
    """The same pair, ranked for credit, comes out the other way -- and must.
    A `rejected` row is the WORST evidence of good work and the BEST reason to
    keep a duplicate's history. Naming the two questions apart is the fix; a
    single rule for both would be wrong at one site or the other."""
    assert memory_rules.evidence_rank("tests_passed") > memory_rules.evidence_rank("rejected")
    assert memory_rules.retention_rank(-0.5, None) < memory_rules.retention_rank(None, 1.0)
