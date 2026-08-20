from sonder_runtime.domain.doctor_result import normalize_result
import sonder_doctor


def test_doctor_compatibility_alias_preserves_identity():
    assert sonder_doctor._normalize_result is normalize_result


def test_mapping_result_uses_explicit_name_and_stringifies_detail():
    assert normalize_result(
        "config", {"name": "configuration", "status": "healthy", "detail": 7}
    ) == {"name": "configuration", "status": "ok", "detail": "7"}


def test_tuple_result_keeps_registry_name_and_handles_none_detail():
    assert normalize_result("ollama", ("warning", None)) == {
        "name": "ollama", "status": "warn", "detail": ""
    }


def test_scalar_and_missing_status_fail_closed():
    assert normalize_result("config", "unknown") == {
        "name": "config", "status": "fail", "detail": ""
    }
    assert normalize_result("config", {}) == {
        "name": "config", "status": "fail", "detail": ""
    }
