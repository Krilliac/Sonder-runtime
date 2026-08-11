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
    """A record_fn that captures what it was asked to write.

    Signature-agnostic on purpose, and not merely as tidiness. Many of the
    assertions in this file and in its dispatch sibling are *negative* --
    ``assert written == []``, the guards that stop a verdict being invented --
    while `attribute`'s production caller (`server._feed_grounded_outcome`)
    wraps the whole call in `except Exception: pass`. A double that raised
    would be swallowed and read as "nothing was written", so every negative
    guard would pass while testing nothing. Measured with a stale
    two-parameter double against a call site passing one extra keyword: 8
    tests failed and 5 passed -- and the 5 that passed were the negative ones.
    """
    written = []
    return written, lambda *a, **k: written.append(tuple(a[:2]))


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


def test_a_different_run_id_does_not_match_even_in_the_same_project():
    """Two concurrent runs (e.g. two autopilot threads) can share a project.
    Project + time window alone cannot tell them apart; run identity must."""
    written, record = _sink()
    go.note_generation("i1", "sonder", project="p", run_id="run-a")

    report = go.attribute("test_run", ok=False, project="p", record_fn=record, run_id="run-b")

    assert report["attributed"] is False
    assert written == []


def test_an_unscoped_run_id_still_matches():
    """Only two *different named* run ids are a mismatch (mirrors project)."""
    written, record = _sink()
    go.note_generation("i1", "sonder", run_id="")

    assert go.attribute("test_run", ok=True, record_fn=record,
                        run_id="run-a")["attributed"] is True


def test_run_id_picks_the_matching_generation_over_a_newer_unrelated_one():
    """Without run scoping, _candidate always returns the newest pending
    generation -- which is exactly the cross-run contamination the run_id
    parameter exists to prevent when two runs interleave."""
    written, record = _sink()
    go.note_generation("older-own", "sonder", run_id="run-a")
    go.note_generation("newer-other-run", "sonder", run_id="run-b")

    report = go.attribute("test_run", ok=True, record_fn=record, run_id="run-a")

    assert report["attributed"] is True
    assert written == [("older-own", "tests_passed")]


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


# --- verifications that never measured anything ---------------------------

# `harness_tools._run` returns returncode -1 for both a timeout and a missing
# binary, and the detect-a-build-system helpers return an `error` dict without
# ever spawning anything. All three used to arrive here as ok=False.

_TIMED_OUT = {"ok": False, "returncode": -1, "timed_out": True, "stderr": ""}
_NO_TOOLCHAIN = {"ok": False, "returncode": -1, "timed_out": False,
                 "stderr": "command not found: mypy"}
_NO_BUILD_SYSTEM = {"ok": False, "error": "no recognized build system found at /x"}
_REAL_FAILURE = {"ok": False, "returncode": 1, "timed_out": False,
                 "stderr": "2 tests failed"}


def test_a_timed_out_verification_records_nothing():
    """MAX_TIMEOUT is a hard 120s clamp, so a suite slower than two minutes
    always times out. Filing `failed` (-1.0) for that measures the clock."""
    written, record = _sink()
    go.note_generation("i1", "sonder")

    report = go.attribute("test_run", ok=False, record_fn=record, evidence=_TIMED_OUT)

    assert report["attributed"] is False
    assert written == []
    assert "timed out" in report["evaluation_infrastructure_error"]


def test_a_missing_toolchain_records_nothing():
    """No mypy on PATH says nothing whatever about the generated code."""
    written, record = _sink()
    go.note_generation("i1", "sonder")

    go.attribute("typecheck_run", ok=False, record_fn=record, evidence=_NO_TOOLCHAIN)

    assert written == []


def test_an_unrecognised_build_system_records_nothing():
    written, record = _sink()
    go.note_generation("i1", "sonder")

    go.attribute("build_run", ok=False, record_fn=record, evidence=_NO_BUILD_SYSTEM)

    assert written == []


def test_unmeasured_is_not_recorded_as_success_either():
    """The third state is unmeasured. Not bad, and equally not good --
    'the tests never ran' must not become `tests_passed`."""
    written, record = _sink()
    go.note_generation("i1", "sonder")

    report = go.attribute("test_run", ok=False, record_fn=record, evidence=_TIMED_OUT)

    assert written == []
    assert "signal" not in report


def test_an_unmeasured_verification_leaves_the_generation_judgeable():
    """It consumed no verdict, so it must not consume the one chance this
    generation had to get a real one."""
    go.note_generation("i1", "sonder")
    go.attribute("test_run", ok=False, evidence=_TIMED_OUT)

    written, record = _sink()
    go.attribute("test_run", ok=False, record_fn=record, evidence=_REAL_FAILURE)

    assert written == [("i1", "failed")]


def test_an_error_with_no_message_is_still_an_error():
    """``{"error": ""}`` is what a message-less exception forwards.

    The wrappers pass ``evidence={"error": str(exc)}``, and ``str()`` on an
    exception raised with no argument is "". Reading the VALUE's truthiness
    made that indistinguishable from "no error was reported", so the whole
    unmeasured state hinged on whether the exception happened to carry a
    message. The KEY is the assertion; the message is only the detail.
    """
    written, record = _sink()
    go.note_generation("i1", "sonder")

    report = go.attribute("test_run", ok=False, record_fn=record,
                          evidence={"ok": False, "error": ""})

    assert written == []
    assert report["attributed"] is False
    assert report["evaluation_infrastructure_error"], (
        "an unmeasured run must still say why it measured nothing"
    )


def test_an_absent_error_key_is_not_an_error():
    """The counterpart: a verifier that ran and reported no error at all must
    keep attributing. Only a PRESENT, non-None error key claims the run never
    produced a verdict."""
    written, record = _sink()
    go.note_generation("i1", "sonder")

    go.attribute("test_run", ok=False, record_fn=record,
                 evidence={"ok": False, "returncode": 1, "error": None})

    assert written == [("i1", "failed")]


def test_a_genuine_failure_is_still_recorded():
    """Failing tests are the whole point of the module; only the runs that
    never produced a verdict are dropped."""
    written, record = _sink()
    go.note_generation("i1", "sonder")

    go.attribute("test_run", ok=False, record_fn=record, evidence=_REAL_FAILURE)

    assert written == [("i1", "failed")]


def test_evidence_is_optional_for_verifiers_that_do_not_run_a_process():
    written, record = _sink()
    go.note_generation("i1", "artifact_generate")

    go.attribute("artifact_verify", ok=True, record_fn=record, evidence=None)

    assert written == [("i1", "compiled")]


def test_unmeasured_runs_are_counted_separately_from_failures():
    _written, record = _sink()
    go.note_generation("i1", "sonder")
    go.attribute("test_run", ok=False, record_fn=record, evidence=_TIMED_OUT)

    stats = go.stats()
    assert stats["unmeasured"] == 1
    assert stats["attributed"] == 0
    assert stats["unlinked"] == 0, "there was a generation; the verifier is what failed"


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
