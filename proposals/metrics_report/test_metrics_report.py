import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import memory_store as ms  # noqa: E402  (repo root, read-only)
import metrics_report as mr  # noqa: E402


def _conn():
    return ms.connect(":memory:")


# --- lesson_source_prefix / lesson_source_breakdown -------------------------

def test_source_prefix_grounded_interaction():
    assert mr.lesson_source_prefix("deadbeefcafef00d", is_grounded=True) == "interaction"


def test_source_prefix_colon_tag_takes_first_segment():
    assert mr.lesson_source_prefix("seed:curriculum:strings:basic", is_grounded=False) == "seed"


def test_source_prefix_no_colon_tag_is_itself():
    assert mr.lesson_source_prefix("community", is_grounded=False) == "community"


def test_source_prefix_missing_is_unknown():
    assert mr.lesson_source_prefix(None, is_grounded=False) == "unknown"
    assert mr.lesson_source_prefix("", is_grounded=False) == "unknown"


def test_lesson_source_breakdown_mixed():
    c = _conn()
    ms.log_interaction(c, "iid1", "task", "ctx", "resp", "code")
    ms.add_lesson(c, ms.new_id(), "Use functools.lru_cache to memoize.", None, "iid1")
    ms.add_lesson(c, ms.new_id(), "Use collections.deque for O(1) pops.", None,
                  "seed:curriculum:strings:basic")
    ms.add_lesson(c, ms.new_id(), "Prefer re.finditer over repeated re.search.", None,
                  "seed:realwork:mangos")
    ms.add_lesson(c, ms.new_id(), "Batch writes inside one transaction.", None, "community")
    breakdown = mr.lesson_source_breakdown(c)
    assert breakdown == {"interaction": 1, "seed": 2, "community": 1}


def test_lesson_source_breakdown_empty_store():
    assert mr.lesson_source_breakdown(_conn()) == {}


# --- outcome_signal_distribution --------------------------------------------

def test_outcome_signal_distribution_counts_and_rewards():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.log_interaction(c, "i2", "t2", "ctx", "resp", "code")
    ms.log_interaction(c, "i3", "t3", "ctx", "resp", "code")
    ms.record_outcome_row(c, "i1", "tests_passed", 1.0)
    ms.record_outcome_row(c, "i2", "tests_passed", 1.0)
    ms.record_outcome_row(c, "i3", "failed", -1.0)
    dist = mr.outcome_signal_distribution(c)
    assert dist["by_signal"]["tests_passed"]["count"] == 2
    assert dist["by_signal"]["tests_passed"]["avg_reward"] == 1.0
    assert dist["by_signal"]["failed"]["count"] == 1
    assert dist["total"] == 3
    assert dist["good_total"] == 2  # tests_passed is good, failed is not
    assert abs(dist["good_fraction"] - (2 / 3)) < 1e-9


def test_outcome_signal_distribution_empty_store():
    dist = mr.outcome_signal_distribution(_conn())
    assert dist == {"by_signal": {}, "total": 0, "good_total": 0, "good_fraction": 0.0}


# --- lessons_per_interaction / distillation_yield ---------------------------

def test_lessons_per_interaction_ratio():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.log_interaction(c, "i2", "t2", "ctx", "resp", "code")
    ms.add_lesson(c, ms.new_id(), "Use bisect.insort for a sorted list.", None, "i1")
    stats = mr.lessons_per_interaction(c)
    assert stats["n_interactions"] == 2
    assert stats["n_lessons"] == 1
    assert stats["lessons_per_interaction"] == 0.5


def test_lessons_per_interaction_no_interactions_is_zero_not_error():
    stats = mr.lessons_per_interaction(_conn())
    assert stats["n_interactions"] == 0
    assert stats["lessons_per_interaction"] == 0.0
    assert stats["distillation_yield"] is None


def test_distillation_yield_uses_good_outcome_interactions_only():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.log_interaction(c, "i2", "t2", "ctx", "resp", "code")
    # i1 has a good outcome and got a lesson distilled; i2 failed and got none.
    ms.record_outcome_row(c, "i1", "tests_passed", 1.0)
    ms.record_outcome_row(c, "i2", "failed", -1.0)
    ms.add_lesson(c, ms.new_id(), "Use heapq.nsmallest instead of sort()[:n].", None, "i1")
    stats = mr.lessons_per_interaction(c)
    assert stats["n_good_outcome_interactions"] == 1
    assert stats["distillation_yield"] == 1.0  # 1 lesson / 1 good-outcome interaction


def test_distillation_yield_below_one_when_some_good_outcomes_yield_nothing():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.log_interaction(c, "i2", "t2", "ctx", "resp", "code")
    ms.record_outcome_row(c, "i1", "tests_passed", 1.0)
    ms.record_outcome_row(c, "i2", "accepted", 0.8)  # also "good" but no lesson stored (e.g. vague)
    ms.add_lesson(c, ms.new_id(), "Use itertools.groupby after sorting the key.", None, "i1")
    stats = mr.lessons_per_interaction(c)
    assert stats["n_good_outcome_interactions"] == 2
    assert stats["distillation_yield"] == 0.5


# --- build_report / format_report -------------------------------------------

def test_build_report_assembles_all_sections():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.record_outcome_row(c, "i1", "tests_passed", 1.0)
    ms.add_lesson(c, ms.new_id(), "Use contextlib.suppress to swallow a known exception.",
                  None, "i1")
    report = mr.build_report(c)
    assert report["n_interactions"] == 1
    assert report["n_lessons"] == 1
    assert report["lesson_sources"] == {"interaction": 1}
    assert report["outcome_signals"]["total"] == 1
    assert report["distillation_yield"] == 1.0


def test_build_report_on_empty_store_has_no_errors():
    report = mr.build_report(_conn())
    assert report["n_interactions"] == 0
    assert report["n_lessons"] == 0
    assert report["lesson_sources"] == {}
    assert report["distillation_yield"] is None


def test_format_report_is_readable_text_with_key_sections():
    c = _conn()
    ms.log_interaction(c, "i1", "t1", "ctx", "resp", "code")
    ms.record_outcome_row(c, "i1", "tests_passed", 1.0)
    ms.add_lesson(c, ms.new_id(), "Use array.array for a compact numeric buffer.", None, "i1")
    text = mr.format_report(mr.build_report(c))
    assert "sonder metrics report" in text
    assert "lessons/interaction" in text
    assert "distillation yield" in text
    assert "lesson sources" in text
    assert "interaction" in text
    assert "outcome signals" in text
    assert "tests_passed" in text


def test_format_report_handles_empty_store_without_crashing():
    text = mr.format_report(mr.build_report(_conn()))
    assert "(none yet)" in text
    assert "n/a (no good outcomes yet)" in text
