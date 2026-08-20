from sonder_runtime.domain.doctor_status import coerce_status
import sonder_doctor


def test_status_policy_is_the_doctor_compatibility_owner():
    assert sonder_doctor._coerce_status is coerce_status


def test_status_policy_normalizes_canonical_values_and_synonyms():
    cases = {
        " OK ": "ok",
        "passed": "ok",
        "degraded": "warn",
        "critical": "fail",
        "skip": "skipped",
    }
    assert {value: coerce_status(value) for value in cases} == cases


def test_status_policy_fails_closed_for_unknown_and_non_boolean_values():
    assert coerce_status("mystery") == "fail"
    assert coerce_status(None) == "fail"
    assert coerce_status(1) == "fail"


def test_status_policy_handles_boolean_verdicts():
    assert coerce_status(True) == "ok"
    assert coerce_status(False) == "fail"
