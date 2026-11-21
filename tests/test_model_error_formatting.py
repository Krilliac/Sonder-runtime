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


def test_structured_error_values_get_the_bearer_and_api_key_scrub():
    detail = safe_model_error_detail(
        {"error": {"message": "Invalid header Authorization: Bearer sk-live-abc123"}},
    )
    assert "sk-live-abc123" not in detail
    detail = safe_model_error_detail([{"message": "invalid api_key sk-live-abc123"}])
    assert "sk-live-abc123" not in detail
    # Context terms are kept in structured bodies as well.
    detail = safe_model_error_detail({"message": "12000 tokens exceeds the 8192 token context"})
    assert "8192 token context" in detail


def test_structured_error_redacts_apikey_cookie_and_auth_keys():
    redacted = redact_model_error_value(
        {"apikey": "k1", "Cookie": "sid=1", "x-auth": "a1", "code": 401},
    )
    assert redacted == {"apikey": "<redacted>", "Cookie": "<redacted>",
                        "x-auth": "<redacted>", "code": 401}


def test_structured_error_with_non_json_leaves_does_not_raise():
    detail = safe_model_error_detail({"detail": b"bearer abcdefgh", "tags": {"x"}})
    assert "abcdefgh" not in detail
    assert detail.startswith("{")
