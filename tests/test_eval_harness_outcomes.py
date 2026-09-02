"""The harness outcome vocabulary is pure and never merges graded with infrastructure.

``harness_outcomes`` owns the status set the eval harness reports, the fold
from ``k`` trials to pass@1 / pass@k, and the classification of one run
against another. Everything here is arithmetic over strings; the harness
tests cover how the runner arrives at those strings.
"""
from __future__ import annotations

import pytest

from sonder_runtime.application.evaluation import harness_outcomes as ho

pytestmark = pytest.mark.unit


def test_every_status_is_graded_or_infrastructure_and_never_both():
    assert set(ho.STATUSES) == ho.GRADED | ho.INFRASTRUCTURE
    assert not (ho.GRADED & ho.INFRASTRUCTURE)
    assert ho.GRADED == {"pass", "fail"}
    assert ho.INFRASTRUCTURE == {
        "error", "timeout", "verifier_unavailable", "unknown", "abandoned",
    }


def test_totals_count_every_status_and_rate_only_the_graded_ones():
    totals = ho.totals(["pass", "fail", "error", "timeout", "verifier_unavailable",
                        "unknown", "abandoned", "pass"])
    assert totals["cases"] == 8
    assert totals["pass"] == 2 and totals["fail"] == 1
    assert totals["graded"] == 3 and totals["infra"] == 5
    assert totals["pass_rate"] == pytest.approx(2 / 3)
    for status in ho.INFRASTRUCTURE:
        assert totals[status] == 1


def test_a_run_with_nothing_graded_has_no_pass_rate():
    assert ho.totals(["error"])["pass_rate"] is None
    assert ho.totals([])["pass_rate"] is None


def test_an_unknown_status_is_rejected_rather_than_counted():
    with pytest.raises(ValueError):
        ho.totals(["passed"])
    with pytest.raises(ValueError):
        ho.validate_status("ok")
    assert ho.is_graded("fail") and ho.is_infrastructure("abandoned")


def test_trials_fold_to_pass_at_1_and_pass_at_k():
    folded = ho.aggregate_trials(["fail", "pass", "error"])
    assert folded == {
        "status": "fail", "trials": ["fail", "pass", "error"], "k": 3,
        "passes": 1, "pass_at_1": False, "pass_at_k": True,
    }
    assert ho.aggregate_trials(["pass"])["pass_at_1"] is True
    with pytest.raises(ValueError):
        ho.aggregate_trials([])


@pytest.mark.parametrize("before, after, verdict", [
    ("pass", "pass", "same"), ("fail", "fail", "same"),
    ("pass", "fail", "regressed"), ("fail", "pass", "improved"),
    ("pass", "error", "infra"), ("timeout", "pass", "infra"),
    ("unknown", "abandoned", "infra"), ("verifier_unavailable", "fail", "infra"),
])
def test_one_case_is_classified_by_its_two_statuses(before, after, verdict):
    assert ho.classify_pair(before, after) == verdict


def test_a_comparison_names_exactly_the_regressed_case_and_fails_for_it():
    before = {"a": {"status": "pass", "trajectory_digest": "x"},
              "b": {"status": "pass", "trajectory_digest": "y"},
              "c": {"status": "fail"}}
    after = {"a": {"status": "pass", "trajectory_digest": "x"},
             "b": {"status": "fail", "trajectory_digest": "z"},
             "c": {"status": "pass"}}
    out = ho.compare_cases(
        before, after, before_run_id="a" * 64, after_run_id="b" * 64,
        before_suite_hash="h", after_suite_hash="h", before_pass_rate=2 / 3)

    assert out["schema"] == ho.COMPARISON_SCHEMA
    assert out["regressed"] == ["b"] and out["improved"] == ["c"] and out["infra"] == []
    assert out["passed"] is False
    assert out["reason_codes"] == ["case_regressions", "trajectory_divergence"]
    assert out["assessment"]["regressed_case_ids"] == ["b"]
    assert out["assessment"]["baseline_run_id"] == "a" * 64
    assert out["assessment"]["baseline_pass_rate"] == pytest.approx(2 / 3)
    by_id = {case["scenario"]: case for case in out["cases"]}
    assert by_id["a"]["trajectory"] == "same"
    assert by_id["b"]["trajectory"] == "divergent"
    assert by_id["c"]["trajectory"] == "unknown"


def test_a_comparison_passes_when_nothing_regressed_and_still_names_infra_and_gains():
    before = {"a": {"status": "pass"}, "b": {"status": "fail"}, "c": {"status": "pass"}}
    after = {"a": {"status": "pass"}, "b": {"status": "pass"}, "c": {"status": "error"}}
    out = ho.compare_cases(before, after, before_suite_hash="h", after_suite_hash="h")

    assert out["passed"] is True
    assert out["improved"] == ["b"] and out["infra"] == ["c"]
    assert "infrastructure_outcomes" in out["reason_codes"]
    assert "case_regressions" not in out["reason_codes"]
    assert out["assessment"]["baseline_run_id"] is None


def test_suite_and_case_set_mismatches_block_a_comparison():
    out = ho.compare_cases(
        {"a": {"status": "pass"}},
        {"a": {"status": "pass"}, "b": {"status": "pass"}},
        before_suite_hash="h1", after_suite_hash="h2")

    assert out["passed"] is False
    assert out["reason_codes"][:2] == ["suite_mismatch", "case_set_mismatch"]
    assert [case["scenario"] for case in out["cases"]] == ["a"]


def test_a_pass_rate_drop_is_named_beside_the_regression_that_caused_it():
    before = {"a": {"status": "pass"}, "b": {"status": "pass"}}
    after = {"a": {"status": "pass"}, "b": {"status": "fail"}}
    out = ho.compare_cases(before, after, before_suite_hash="h",
                           after_suite_hash="h", before_pass_rate=1.0)

    assert "pass_rate_drop" in out["reason_codes"]
    assert "case_regressions" in out["reason_codes"]
    assert out["after_pass_rate"] == pytest.approx(0.5)


def test_step_level_divergences_override_the_digest_verdict():
    before = {"a": {"status": "pass", "trajectory_digest": "x"}}
    after = {"a": {"status": "pass", "trajectory_digest": "y"}}

    same = ho.compare_cases(before, after, before_suite_hash="h",
                            after_suite_hash="h", trajectory_divergences={"a": []})
    assert same["cases"][0]["trajectory"] == "same"
    assert "trajectory_divergence" not in same["reason_codes"]

    divergent = ho.compare_cases(
        before, after, before_suite_hash="h", after_suite_hash="h",
        trajectory_divergences={"a": [{"index": 0, "field": "output"}]})
    assert divergent["cases"][0]["trajectory"] == "divergent"
    assert divergent["cases"][0]["divergences"] == [{"index": 0, "field": "output"}]
    assert divergent["passed"] is True, "divergence alone is information, not a failure"


def test_a_baseline_id_that_is_not_a_digest_is_not_pinned():
    out = ho.compare_cases({"a": {"status": "pass"}}, {"a": {"status": "pass"}},
                           before_run_id="not-a-digest", before_suite_hash="h",
                           after_suite_hash="h", before_pass_rate=1.0)
    assert out["before_run_id"] == "not-a-digest"
    assert out["assessment"]["baseline_run_id"] is None
    assert out["assessment"]["baseline_pass_rate"] is None
