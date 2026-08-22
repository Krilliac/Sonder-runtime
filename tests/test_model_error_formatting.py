from sonder_runtime.domain.model_error_formatting import (
    redact_model_error_value,
    safe_model_error_detail,
)


def test_redact_model_error_value_bounds_and_scrubs_nested_credentials():
    value = {
        "message": "failed",
        "api-key": "secret-value",
        "nested": {"Authorization": "Bearer abc123"},
    }

    redacted = redact_model_error_value(value)

    assert redacted["message"] == "failed"
    assert redacted["api-key"] == "<redacted>"
    assert redacted["nested"]["Authorization"] == "<redacted>"


def test_safe_model_error_detail_preserves_context_terms_and_redacts_tokens():
    detail = safe_model_error_detail(
        "12000 tokens exceeds the 8192 token context; token=super-secret-value",
    )

    assert "8192 token context" in detail
    assert "super-secret-value" not in detail


def test_safe_model_error_detail_serializes_structured_values_deterministically():
    detail = safe_model_error_detail({"z": 2, "secret": "hidden", "a": 1})

    assert detail == '{"a": 1, "secret": "<redacted>", "z": 2}'
