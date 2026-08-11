"""Verification results are attributed back to the work that produced them.

The point of the module is to fix a reporting bias, so the tests care most
about the ways attribution can go WRONG: a wrong link poisons the very
population this exists to clean up, and is worse than recording nothing.
"""
import pytest

import grounded_outcomes as go


@pytest.fixture(autouse=True)
def _clean():
    go.reset()
    yield
    go.reset()


def _sink():
    """A record_fn that captures what it was asked to write."""
    written = []
    return written, lambda ident, signal: written.append((ident, signal))


# --- noting work worth judging --------------------------------------------


def test_a_generation_with_no_interaction_id_is_not_judgeable():
    """No id means no row to attach an outcome to; recording it would strand it."""
    assert go.note_generation("", "sonder") is False
    assert go.pending_count() == 0


def test_only_generators_are_noted():
    assert go.note_generation("i1", "sonder") is True
    assert go.note_generation("i2", "file_read") is False   # a read produces nothing
    assert go.pending_count() == 1


def test_regenerating_the_same_interaction_replaces_rather_than_stacks():
    """One id must not be able to collect several verdicts."""
    go.note_generation("i1", "sonder")
    go.note_generation("i1", "sonder")

    assert go.pending_count() == 1


# --- attribution ----------------------------------------------------------


def test_a_passing_test_run_is_recorded_against_the_generation():
    written, record = _sink()
    go.note_generation("i1", "offload", project="p")

    report = go.attribute("test_run", ok=True, project="p", record_fn=record)

    assert report["attributed"] is True
    assert report["signal"] == "tests_passed"
    assert written == [("i1", "tests_passed")]


def test_a_failing_build_records_the_negative_signal():
    """The whole reason this module exists: failures get filed without being asked."""
    written, record = _sink()
    go.note_generation("i1", "codegen_build_loop")

    report = go.attribute("build_run", ok=False, record_fn=record)

    assert report["signal"] == "failed"
    assert written == [("i1", "failed")]


def test_building_is_not_passing():
    """`compiled` sits below the good threshold on purpose."""
    from sonder_runtime.domain.memory import rules

    assert rules.reward_is_good("tests_passed")
    assert not rules.reward_is_good("compiled")
    assert go.VERIFIERS["build_run"][0] == "compiled"
    assert go.VERIFIERS["test_run"][0] == "tests_passed"


def test_a_tool_that_is_not_evidence_never_attributes():
    written, record = _sink()
    go.note_generation("i1", "sonder")

    report = go.attribute("file_read", ok=True, record_fn=record)

    assert report["attributed"] is False
    assert written == []
    assert go.pending_count() == 1, "the generation is still awaiting real evidence"


def test_nothing_pending_means_nothing_recorded():
    written, record = _sink()

    report = go.attribute("test_run", ok=False, record_fn=record)

    assert report["attributed"] is False
    assert written == []


# --- the ways a link would be wrong ---------------------------------------


def test_a_stale_generation_is_never_judged_by_a_much_later_run(monkeypatch):
    written, record = _sink()
    clock = [1000.0]
    monkeypatch.setattr(go, "_now", lambda: clock[0])
    go.note_generation("i1", "sonder")

    clock[0] += go.ATTRIBUTION_WINDOW_SECONDS + 1
    report = go.attribute("test_run", ok=False, record_fn=record)

    assert report["attributed"] is False
    assert written == [], "an unrelated later run must not inherit an old generation"


def test_a_different_named_project_does_not_match():
    written, record = _sink()
    go.note_generation("i1", "sonder", project="alpha")

    report = go.attribute("test_run", ok=False, project="beta", record_fn=record)

    assert report["attributed"] is False
    assert written == []


def test_an_unscoped_side_still_matches():
    """Only two *different named* projects are a mismatch."""
    written, record = _sink()
    go.note_generation("i1", "sonder", project="")

    assert go.attribute("test_run", ok=True, project="alpha",
                        record_fn=record)["attributed"] is True


def test_one_verification_kind_judges_a_generation_only_once():
    written, record = _sink()
    go.note_generation("i1", "sonder")

    first = go.attribute("test_run", ok=True, record_fn=record)
    second = go.attribute("test_run", ok=False, record_fn=record)

    assert first["attributed"] is True
    assert second["attributed"] is False, "a rerun must not double-count the same id"
    assert written == [("i1", "tests_passed")]


def test_a_different_verification_kind_may_still_judge_it():
    written, record = _sink()
    go.note_generation("i1", "sonder")

    go.attribute("build_run", ok=True, record_fn=record)
    go.attribute("test_run", ok=False, record_fn=record)

    assert written == [("i1", "compiled"), ("i1", "failed")]


def test_the_newest_generation_is_judged_first():
    written, record = _sink()
    go.note_generation("old", "sonder")
    go.note_generation("new", "sonder")

    go.attribute("test_run", ok=True, record_fn=record)

    assert written == [("new", "tests_passed")]


# --- robustness -----------------------------------------------------------


def test_a_failed_write_is_reported_and_does_not_raise():
    """Bookkeeping must never take down the run it is observing."""
    def _explode(_ident, _signal):
        raise RuntimeError("db locked")

    go.note_generation("i1", "sonder")
    report = go.attribute("test_run", ok=True, record_fn=_explode)

    assert report["attributed"] is True
    assert report["recorded"] is False
    assert "db locked" in report["error"]


def test_the_ledger_is_bounded():
    for index in range(go.MAX_PENDING + 25):
        go.note_generation("i%d" % index, "sonder")

    assert go.pending_count() <= go.MAX_PENDING


def test_attribution_works_without_a_record_function():
    """Callers may want the decision without the write."""
    go.note_generation("i1", "sonder")

    report = go.attribute("test_run", ok=True)

    assert report["attributed"] is True
    assert "recorded" not in report


def test_stats_count_what_happened():
    _written, record = _sink()
    go.note_generation("i1", "sonder")
    go.attribute("test_run", ok=True, record_fn=record)
    go.attribute("test_run", ok=True, record_fn=record)   # nothing left to judge

    stats = go.stats()
    assert stats["noted"] == 1
    assert stats["attributed"] == 1
    assert stats["unlinked"] == 1


# --- the runtime must not mark its own homework ----------------------------
#
# codegen_build_loop is the one tool listed in BOTH GENERATORS and VERIFIERS:
# it writes the code and then runs the compiler over it. Nothing in this module
# stopped it attributing its own `compiled` (+0.70) to its own generation --
# the only thing that did was the `if/elif` ordering at the single call site in
# server.py, which is a precondition in another file that attribute()'s callers
# cannot see. 4e2a315 fixed exactly this shape in reward.py: an ordering that is
# "harmless only while every call site happens to be gated".


def test_the_two_role_sets_overlap_on_exactly_one_tool():
    """Pinned so a second dual-role tool cannot be added unnoticed."""
    assert sorted(set(go.GENERATORS) & set(go.VERIFIERS)) == ["codegen_build_loop"]


def test_a_tool_never_grades_the_work_it_generated_itself():
    written, record = _sink()
    go.note_generation("i1", "codegen_build_loop", project="p")

    report = go.attribute("codegen_build_loop", ok=True, project="p", record_fn=record)

    assert report["attributed"] is False
    assert written == [], "self-graded evidence is the population this module exists to dilute"
    # "I refused to grade my own work" and "there was nothing waiting to be
    # graded" are different facts about a run. Folding the first into
    # `unlinked` is the metric-blending shape at small scale: a consumer
    # reading `unlinked` alone cannot tell a working guard from an idle module.
    stats = go.stats()
    assert stats["self_blocked"] == 1
    assert stats["unlinked"] == 0


def test_a_self_generated_row_does_not_hide_an_eligible_older_one():
    """The guard must skip its own row and keep looking, not give up."""
    written, record = _sink()
    go.note_generation("older", "offload", project="p")
    go.note_generation("newer", "codegen_build_loop", project="p")

    report = go.attribute("codegen_build_loop", ok=True, project="p", record_fn=record)

    assert report["attributed"] is True
    assert written == [("older", "compiled")]


def test_another_verifier_may_still_judge_a_codegen_generation():
    """The guard is about self-grading, not about codegen work being unjudgeable."""
    written, record = _sink()
    go.note_generation("i1", "codegen_build_loop", project="p")

    go.attribute("test_run", ok=True, project="p", record_fn=record)

    assert written == [("i1", "tests_passed")]


# --- a write that failed did not happen ------------------------------------


def test_a_failed_write_leaves_the_generation_still_judgeable():
    """attribute() consumed the pending before the write, so a locked database
    lost the row permanently AND silently: the caller discards the report, and
    the same verifier can never claim that generation again."""
    def _explode(_ident, _signal):
        raise RuntimeError("db locked")

    written, record = _sink()
    go.note_generation("i1", "sonder", project="p")

    go.attribute("test_run", ok=True, project="p", record_fn=_explode)
    retry = go.attribute("test_run", ok=True, project="p", record_fn=record)

    assert retry["attributed"] is True
    assert written == [("i1", "tests_passed")]


def test_stats_count_rows_written_apart_from_attribution_decisions():
    """`attributed` was incremented before the write, so it counted intent."""
    def _explode(_ident, _signal):
        raise RuntimeError("db locked")

    _written, record = _sink()
    go.note_generation("i1", "sonder", project="p")
    go.attribute("test_run", ok=True, project="p", record_fn=_explode)
    go.attribute("test_run", ok=True, project="p", record_fn=record)

    stats = go.stats()
    # The split this test is named for: two calls each reached the write, so
    # two DECISIONS were made, and exactly one ROW landed. Asserting only the
    # new counters left the old one -- the one that used to mean both -- unpinned.
    assert stats.get("attributed") == 2
    assert stats.get("write_failed") == 1
    assert stats.get("recorded") == 1
