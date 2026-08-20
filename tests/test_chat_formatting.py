from sonder_runtime.adapters.observability.chat_formatting import chat_usage


def test_chat_usage_normalizes_token_counts():
    assert chat_usage({"tokens_in": "12", "tokens_out": 8}) == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }

def test_chat_usage_clamps_negative_counts():
    assert chat_usage({"tokens_in": -4, "tokens_out": -2}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_chat_usage_rejects_malformed_activity_without_failing_response():
    assert chat_usage({"tokens_in": object(), "tokens_out": "not-a-number"}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
