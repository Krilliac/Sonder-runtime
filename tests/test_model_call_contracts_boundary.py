"""Model-call contracts live in the adapters layer; root names are aliases."""
import pytest

import server
from sonder_runtime.adapters import model_response_metadata, offload_schema_argument
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.model_response_detail import empty_model_response_detail


def test_root_names_are_identity_preserving_aliases():
    assert server._response_error_metadata is model_response_metadata.response_error_metadata
    assert server._parse_schema_arg is offload_schema_argument.parse_schema_arg


def test_metadata_is_read_only_from_the_allowlisted_empty_response_detail():
    read = model_response_metadata.response_error_metadata
    detail = empty_model_response_detail(
        {"eval_count": 3, "done_reason": "length"}, {"thinking": "abc", "tool_calls": []},
    )
    assert read(ModelCallError("empty_response", detail)) == {"thinking_chars": 3, "done_reason": "length"}
    assert read(ModelCallError("empty_response", "Ollama returned no assistant content")) == {}
    assert read(ModelCallError("http", detail, status=500)) == {}
    assert read(ModelCallError("empty_response", "Ollama returned no assistant content; metadata=[1]")) == {}
    assert read(RuntimeError(detail)) == {}
    weird = ModelCallError(
        "empty_response",
        'Ollama returned no assistant content; metadata={"done_reason": "weird", "thinking_chars": 0}',
    )
    assert read(weird) == {"done_reason": "other"}


def test_schema_argument_normalizes_or_raises_a_typed_configuration_error():
    parse = offload_schema_argument.parse_schema_arg
    assert parse(None) is None
    assert parse("   ") is None
    assert parse({"type": "object"}) == {"type": "object"}
    assert parse('{"type": "object", "required": ["a"]}') == {"type": "object", "required": ["a"]}
    expectations = {
        "not json": "schema is not valid JSON",
        "[1, 2]": "schema must be a JSON object, got list",
        42: "schema must be a JSON object or JSON text, got int",
    }
    for bad, message in expectations.items():
        with pytest.raises(ModelCallError) as excinfo:
            parse(bad)
        assert excinfo.value.kind == "configuration"
        assert excinfo.value.detail.startswith(message)
