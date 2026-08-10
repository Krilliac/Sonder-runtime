"""Reliability is counted, not felt -- and ignorance is not a pass.

The properties worth defending here are the two that keep the number honest:
populations are never averaged, and a thin sample fails closed.
"""
import pytest

import calibration


def _with_counts(monkeypatch, counts):
    monkeypatch.setattr(calibration, "_counts", lambda _conn: dict(counts))
    return object()   # a stand-in connection; _counts is what reads it


# --- the two honesty rules ------------------------------------------------


def test_the_two_populations_are_never_combined(monkeypatch):
    """A figure over both reads like accuracy and is not one.

    These are the live store's real proportions: a self-graded curriculum that
    dwarfs the judged sample. Combined they read ~96%; apart they say 53% and
    98%, which are different facts about different questions.
    """
    conn = _with_counts(monkeypatch, {
        "tests_passed": 8883, "failed": 182, "compiled": 1,
        "accepted": 32, "edited": 60, "used": 9, "rejected": 91,
    })

    caller = calibration.measure(conn, "caller")
    execution = calibration.measure(conn, "execution")

    assert caller.total == 192
    assert round(caller.rate, 3) == 0.526
    assert caller.verdict == calibration.POOR
    assert execution.rate > 0.95
    assert execution.verdict == calibration.GOOD
    # There must be no way to ask for one number over everything.
    assert not hasattr(calibration, "overall")
    assert not hasattr(calibration, "combined")


def test_choosing_a_population_is_mandatory(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 50})

    with pytest.raises(ValueError) as excinfo:
        calibration.measure(conn, "everything")

    assert "caller" in str(excinfo.value) and "execution" in str(excinfo.value)


def test_too_little_evidence_is_unmeasured_not_good(monkeypatch):
    """'No evidence of failure' must not become 'evidence of no failure'."""
    conn = _with_counts(monkeypatch, {"accepted": 3})

    m = calibration.measure(conn, "caller")

    assert m.verdict == calibration.UNMEASURED
    assert m.rate is None
    assert m.total < calibration.MIN_SAMPLE


def test_ignorance_demands_verification_exactly_as_unreliability_does(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 3})

    verify, reason = calibration.should_verify(conn, "caller")

    assert verify is True
    assert "unknown" in reason


def test_a_perfect_but_tiny_record_still_fails_closed(monkeypatch):
    """100% of 5 is not a track record."""
    conn = _with_counts(monkeypatch, {"accepted": 5})

    assert calibration.should_verify(conn, "caller")[0] is True


# --- the decision that hangs off the number -------------------------------


def test_a_poor_record_forces_verification(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 40, "rejected": 60})

    verify, reason = calibration.should_verify(conn, "caller")

    assert verify is True
    assert "40%" in reason


def test_a_middling_record_proceeds_but_still_checks(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 70, "rejected": 30})

    verify, reason = calibration.should_verify(conn, "caller")

    assert verify is True
    assert "not good enough to skip checking" in reason


def test_a_good_record_stops_demanding_verification(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 95, "rejected": 5})

    verify, reason = calibration.should_verify(conn, "caller")

    assert verify is False
    assert "95%" in reason


def test_caution_is_silent_when_the_record_does_not_warrant_it(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 95, "rejected": 5})

    assert calibration.caution(conn, "caller") == ""


def test_caution_says_something_measured_when_it_does(monkeypatch):
    conn = _with_counts(monkeypatch, {"accepted": 40, "rejected": 60})

    line = calibration.caution(conn, "caller")

    # Every word of it is a projection of the counts; nothing is generated.
    assert "40%" in line and "100" in line


# --- counting -------------------------------------------------------------


def test_good_and_bad_follow_the_shared_reward_table(monkeypatch):
    """The split must come from the rules table, not a second opinion here."""
    from sonder_runtime.domain.memory import rules

    conn = _with_counts(monkeypatch, {
        "used": 1, "copied": 1, "edited": 1, "accepted": 1, "rejected": 1,
    })
    m = calibration.measure(conn, "caller")

    expected_good = sum(
        1 for s in ("used", "copied", "edited", "accepted") if rules.reward_is_good(s)
    )
    assert m.good == expected_good
    assert m.bad == 1


def test_signals_outside_the_population_are_ignored(monkeypatch):
    conn = _with_counts(monkeypatch, {
        "accepted": 30, "rejected": 10, "tests_passed": 9999,
    })

    m = calibration.measure(conn, "caller")

    assert m.total == 40, "curriculum runs must not inflate the judged sample"


def test_an_empty_store_is_unmeasured_rather_than_an_error(monkeypatch):
    conn = _with_counts(monkeypatch, {})

    m = calibration.measure(conn, "caller")

    assert m.verdict == calibration.UNMEASURED
    assert m.total == 0
    assert calibration.should_verify(conn, "caller")[0] is True


def test_a_broken_store_degrades_to_unmeasured():
    """A reporting failure must not read as a good record.

    `object()` is not a connection, so the real `_counts` raises internally and
    swallows it. The result must be "unknown", never "fine".
    """
    m = calibration.measure(object(), "caller")

    assert m.verdict == calibration.UNMEASURED
    assert calibration.should_verify(object(), "caller")[0] is True


# --- rendering ------------------------------------------------------------


def test_the_report_shows_both_populations_and_the_decision(monkeypatch):
    conn = _with_counts(monkeypatch, {
        "tests_passed": 8883, "failed": 182,
        "accepted": 32, "edited": 60, "used": 9, "rejected": 91,
    })

    text = calibration.report(conn)

    assert "caller:" in text and "execution:" in text
    assert "verify before claiming done: yes" in text
    assert "not averaged on purpose" in text


def test_measurement_str_is_readable_in_both_states(monkeypatch):
    thin = _with_counts(monkeypatch, {"accepted": 2})
    assert "too few to measure" in str(calibration.measure(thin, "caller"))

    thick = _with_counts(monkeypatch, {"accepted": 90, "rejected": 10})
    assert "90.0%" in str(calibration.measure(thick, "caller"))
