"""Empty model response detail lives in the domain; the root name is an alias."""
import server
from sonder_runtime.domain import model_response_detail as detail


def test_root_helper_is_an_identity_preserving_alias():
    assert server._empty_model_response_detail is detail.empty_model_response_detail


def test_detail_reports_only_shape_metadata_never_reasoning_text():
    out = {"eval_count": 12, "done_reason": "Length"}
    message = {"thinking": "secret chain of thought", "tool_calls": [{"a": 1}, {"b": 2}], "content": ""}
    text = detail.empty_model_response_detail(out, message)
    assert text == (
        'Ollama returned no assistant content; metadata={"done_reason": "length", '
        '"eval_count": 12, "thinking_chars": 23, "tool_call_count": 2}'
    )
    assert "secret" not in text


def test_missing_or_unusual_fields_degrade_to_the_bare_message():
    assert detail.empty_model_response_detail({}, None) == "Ollama returned no assistant content"
    assert detail.empty_model_response_detail({"eval_count": "x", "done_reason": "  "}, {"thinking": 5}) == (
        "Ollama returned no assistant content"
    )
    assert detail.empty_model_response_detail({"done_reason": "weird"}, {}) == (
        'Ollama returned no assistant content; metadata={"done_reason": "other"}'
    )
