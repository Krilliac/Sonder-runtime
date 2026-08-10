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


def test_no_verifier_may_write_into_the_caller_judged_population():
    """A machine verdict is not a caller's judgement.

    Everything in VERIFIERS is a program deciding whether something ran or
    validated. `calibration.CALLER_JUDGED` answers a different question -- did a
    human accept the delegated work -- and it is the population `should_verify`
    and the `_status` thresholds gate on. One machine signal leaking into it
    moves that gate, optimistically, with no human having judged anything.
    """
    import calibration

    for tool, (good, bad) in go.VERIFIERS.items():
        assert good not in calibration.CALLER_JUDGED, tool
        assert bad not in calibration.CALLER_JUDGED, tool
        assert good in calibration.EXECUTION_GROUNDED, tool
        assert bad in calibration.EXECUTION_GROUNDED, tool


def test_an_artifact_validator_files_its_verdict_as_execution_evidence():
    """`artifact_verify` runs a file-format checker: it proves the artifact is
    well formed, which is exactly what `compiled` means and is not what
    `accepted` means."""
    written, record = _sink()
    go.note_generation("i1", "artifact_generate")

    go.attribute("artifact_verify", ok=True, record_fn=record)

    assert written == [("i1", "compiled")]


def test_a_failing_artifact_validator_is_execution_evidence_too():
    written, record = _sink()
    go.note_generation("i1", "artifact_generate")

    go.attribute("artifact_ground", ok=False, record_fn=record)

    assert written == [("i1", "failed")]


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
