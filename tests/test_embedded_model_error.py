from sonder_runtime.domain.model_error_formatting import embedded_model_error


def test_embedded_model_error_ignores_non_mapping_responses_and_missing_errors():
    assert embedded_model_error(None) == ""
    assert embedded_model_error({"message": "ok"}) == ""
    assert embedded_model_error({"error": ""}) == ""


def test_embedded_model_error_formats_scalar_error_and_redacts_credentials():
    detail = embedded_model_error({"error": "Bearer secret-value"})

    assert detail == "Bearer=<redacted>"
    assert "secret-value" not in detail


def test_embedded_model_error_formats_structured_error_deterministically():
    detail = embedded_model_error({"error": {"z": 2, "secret": "hidden", "a": 1}})

    assert detail == '{"a": 1, "secret": "<redacted>", "z": 2}'
