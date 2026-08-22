import sonder_doctor

from sonder_runtime.bootstrap import doctor_formatting
from sonder_runtime.domain import doctor_status


def test_packaged_doctor_formatting_owns_root_aliases():
    assert sonder_doctor.render_report is doctor_formatting.render_report
    assert sonder_doctor.rollup_status is doctor_formatting.rollup_status
    assert sonder_doctor.STATUS_FAIL is doctor_status.STATUS_FAIL
    assert sonder_doctor._SEVERITY is doctor_formatting._SEVERITY


def test_rollup_status_keeps_skipped_neutral_and_fail_dominant():
    assert doctor_formatting.rollup_status([
        {"status": "skipped"}, {"status": "warn"}, {"status": "ok"}
    ]) == "warn"
    assert doctor_formatting.rollup_status([
        {"status": "fail"}, {"status": "warn"}
    ]) == "fail"
    assert doctor_formatting.rollup_status([]) == "ok"


def test_render_report_preserves_plain_text_contract():
    assert doctor_formatting.render_report({
        "overall": "warn",
        "checks": [{"name": "config", "status": "warn", "detail": "watch"}],
    }) == "sonder doctor: WARN\n  [warn] config  watch"
