"""Precision/recall scoring, hand computed on the ten-record synthetic set."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.security_eval.scoring import (
    ChunkOutcome, ScoreRow, paired_comparison, score, score_chunks)

pytestmark = pytest.mark.unit

# hid: (category, stage, label)
META = {
    "h01": ("bounds", "r1", "CONFIRMED"), "h02": ("bounds", "new", "CONFIRMED"),
    "h03": ("bounds", "r1", "REFUTED"), "h04": ("auth", "new", "REFUTED"),
    "h05": ("auth", "r1", "CONFIRMED"), "h06": ("auth", "r1", "UNCERTAIN"),
    "h07": ("race", "new", "REFUTED"), "h08": ("race", "new", "CONFIRMED"),
    "h09": ("race", "r1", "REFUTED"), "h10": ("bounds", "new", "REFUTED"),
}
# hid: (predicted, outcome)
NONE_ARM = {
    "h01": ("CONFIRMED", "SCORED"), "h02": ("REFUTED", "SCORED"), "h03": ("CONFIRMED", "SCORED"),
    "h04": ("REFUTED", "SCORED"), "h05": ("UNCERTAIN", "SCORED"), "h06": ("CONFIRMED", "SCORED"),
    "h07": ("CONFIRMED", "SCORED"), "h08": (None, "UNPARSED"), "h09": ("REFUTED", "SCORED"),
    "h10": (None, "MODEL_ERROR"),
}
RETRIEVED_ARM = {
    "h01": ("CONFIRMED", "SCORED"), "h02": ("CONFIRMED", "SCORED"), "h03": ("REFUTED", "SCORED"),
    "h04": ("REFUTED", "SCORED"), "h05": ("CONFIRMED", "SCORED"), "h06": ("REFUTED", "SCORED"),
    "h07": ("REFUTED", "SCORED"), "h08": ("CONFIRMED", "SCORED"), "h09": ("REFUTED", "SCORED"),
    "h10": ("CONFIRMED", "SCORED"),
}


def rows_for(arm, table):
    out = []
    for hid, (predicted, outcome) in table.items():
        category, stage, label = META[hid]
        out.append(ScoreRow(hid, arm, category, stage, label, predicted, outcome))
    return out


@pytest.fixture
def rows():
    return rows_for("none", NONE_ARM) + rows_for("retrieved", RETRIEVED_ARM)


def test_overall_precision_and_recall_per_arm(rows):
    report = score(rows)
    none = report.overall["none"]
    assert (none.tp, none.fp, none.fn, none.tn) == (1, 2, 3, 3)
    assert none.precision == pytest.approx(1 / 3) and none.recall == pytest.approx(1 / 4)
    retrieved = report.overall["retrieved"]
    assert (retrieved.tp, retrieved.fp, retrieved.fn, retrieved.tn) == (4, 1, 0, 4)
    assert retrieved.precision == pytest.approx(0.8) and retrieved.recall == pytest.approx(1.0)


def test_uncertain_labels_are_excluded_and_counted_and_errors_are_never_dropped(rows):
    none = score(rows).overall["none"]
    assert none.label_uncertain == 1
    assert none.model_uncertain == 1 and none.unparsed == 1 and none.model_error == 1
    assert none.n == 10
    assert none.tp + none.fp + none.fn + none.tn == 9
    # the unparsed CONFIRMED item is a missed positive, not a dropped row
    assert none.fn == 3


def test_per_category_and_per_stage(rows):
    groups = score(rows).groups
    bounds = groups["category"]["bounds"]["none"]
    assert (bounds.tp, bounds.fp, bounds.fn) == (1, 1, 1)
    assert bounds.precision == pytest.approx(0.5) and bounds.recall == pytest.approx(0.5)
    auth = groups["category"]["auth"]["none"]
    assert auth.precision is None and auth.recall == 0.0
    race = groups["category"]["race"]["none"]
    assert race.precision == 0.0 and race.recall == 0.0
    r1 = groups["stage"]["r1"]["none"]
    assert (r1.tp, r1.fp, r1.fn) == (1, 1, 1)
    new = groups["stage"]["new"]["none"]
    assert new.precision == 0.0 and new.recall == 0.0
    assert set(groups) == {"category", "stage"}
    assert groups["stage"]["new"]["retrieved"].precision == pytest.approx(2 / 3)


def test_precision_is_none_not_zero_or_one_when_nothing_was_predicted_confirmed():
    quiet = [ScoreRow("a", "quiet", "c", "r1", "CONFIRMED", "REFUTED", "SCORED"),
             ScoreRow("b", "quiet", "c", "r1", "REFUTED", "REFUTED", "SCORED")]
    confusion = score(quiet).overall["quiet"]
    assert confusion.precision is None
    assert confusion.recall == 0.0
    assert confusion.as_dict()["precision"] is None


def test_recall_is_none_when_the_labels_have_no_confirmed_item():
    only_refuted = [ScoreRow("a", "x", "c", "r1", "REFUTED", "CONFIRMED", "SCORED")]
    confusion = score(only_refuted).overall["x"]
    assert confusion.recall is None and confusion.precision == 0.0


def test_mcnemar_counts_and_exact_p(rows):
    paired = paired_comparison(rows, "none", "retrieved")
    assert (paired.n_pairs, paired.b, paired.c, paired.excluded) == (7, 0, 4, 3)
    assert paired.p_value == pytest.approx(0.125)


def test_mcnemar_two_sided_with_discordance_both_ways():
    def pair(hid, label, none_pred, ret_pred):
        return [ScoreRow(hid, "none", "c", "r1", label, none_pred, "SCORED"),
                ScoreRow(hid, "retrieved", "c", "r1", label, ret_pred, "SCORED")]
    rows = (pair("a", "CONFIRMED", "CONFIRMED", "REFUTED")             # b
            + pair("b", "CONFIRMED", "REFUTED", "CONFIRMED")          # c
            + pair("c", "REFUTED", "CONFIRMED", "REFUTED")            # c
            + pair("d", "REFUTED", "CONFIRMED", "REFUTED")            # c
            + pair("e", "REFUTED", "REFUTED", "REFUTED"))             # concordant
    paired = paired_comparison(rows)
    assert (paired.b, paired.c, paired.n_pairs) == (1, 3, 5)
    assert paired.p_value == pytest.approx(0.625)


def test_mcnemar_with_no_discordant_pairs_has_p_one():
    rows = [ScoreRow("a", "none", "c", "r1", "CONFIRMED", "CONFIRMED", "SCORED"),
            ScoreRow("a", "retrieved", "c", "r1", "CONFIRMED", "CONFIRMED", "SCORED")]
    paired = paired_comparison(rows)
    assert (paired.b, paired.c) == (0, 0) and paired.p_value == 1.0


def test_chunk_scores_label_precision_as_a_floor_and_count_failed_chunks():
    outcomes = [
        ChunkOutcome("none", 4, ("CONFIRMED", "REFUTED"), 3, "SCORED"),
        ChunkOutcome("none", 2, ("CONFIRMED",), 1, "SCORED"),
        ChunkOutcome("none", 0, (), 2, "MODEL_ERROR"),
    ]
    result = score_chunks(outcomes)["none"]
    assert result.generated == 6 and result.matched_confirmed == 2 and result.matched_refuted == 1
    assert result.precision_floor == pytest.approx(2 / 6)
    assert result.precision_labelled == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(2 / 6)
    assert result.chunks == 3 and result.failed_chunks == 1


def test_chunk_precision_is_none_with_nothing_generated():
    result = score_chunks([ChunkOutcome("a", 0, (), 0, "SCORED")])["a"]
    assert result.precision_floor is None and result.precision_labelled is None and result.recall is None


# -- summary ---------------------------------------------------------------------------------

def _report(complete=True):
    rows = rows_for("none", NONE_ARM) + rows_for("retrieved", RETRIEVED_ARM)
    return {
        "schema": 1, "mode": "item", "requested_model": "demo-model:9b", "helper_version": "1",
        "served_models": {"demo-model:9b-q8": 20}, "served_model_source": "gateway_resolved",
        "config_digest": "abc123", "arms": ["none", "retrieved"], "complete": complete,
        "stopped_reason": "done" if complete else "time_budget", "remaining": 0 if complete else 6,
        "rows": 20, "outcomes": {"SCORED": 18, "UNPARSED": 1, "MODEL_ERROR": 1},
        "tokens": {"tokens_in": 1000, "tokens_out": 200, "rows": 20, "rows_measured": 15, "rows_unverified": 5},
        "scores": score(rows).as_dict(), "paired": paired_comparison(rows).as_dict(),
    }


def test_summary_is_short_and_states_the_model_tokens_and_both_arms():
    from sonder_runtime.domain.security_eval.summary import render_summary
    text = render_summary(_report())
    assert len(text.splitlines()) <= 60
    for needle in ("demo-model:9b", "demo-model:9b-q8", "gateway_resolved", "15 of 20", "none", "retrieved",
                   "bounds", "McNemar", "0.125"):
        assert needle in text
    assert "INCOMPLETE" not in text
    assert "n/a" in text  # a precision of None is printed as n/a, not 0 or 1


def test_summary_of_an_incomplete_run_carries_a_banner():
    from sonder_runtime.domain.security_eval.summary import render_summary
    text = render_summary(_report(complete=False))
    assert text.splitlines()[0].startswith("# ") and "INCOMPLETE" in text and "time_budget" in text and "6" in text
