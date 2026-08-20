import sonder_doctor

from sonder_runtime.domain.doctor_result import skipped


def test_skipped_result_owns_the_pure_policy():
    assert skipped("missing config") == {
        "status": "skipped",
        "detail": "skipped: missing config",
    }


def test_root_skip_alias_delegates_to_packaged_policy():
    assert sonder_doctor._skip("offline") == skipped("offline")


def test_skipped_result_stringifies_reason_like_existing_formatting():
    assert skipped(404) == {
        "status": "skipped",
        "detail": "skipped: 404",
    }
