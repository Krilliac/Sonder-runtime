from sonder_runtime.application.protocol.openai_compatibility import (
    CanonicalResponse,
    CompatibilityError,
    OpenAICompatibility,
)


def test_chat_request_normalizes_and_calls_policy_then_event_hooks():
    calls = []
    adapter = OpenAICompatibility(
        policy_hook=lambda operation, payload: calls.append(("policy", operation)) or True,
        event_hook=lambda event: calls.append(("event", event["kind"])),
    )
    result = adapter.request({"model": "sonder", "messages": [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Hi"},
    ]}, operation="chat.completions")
    assert result.messages[-1] == {"role": "user", "content": "Hi"}
    assert calls == [("policy", "chat.completions"), ("event", "openai.request.normalized")]


def test_responses_input_text_and_output_round_trip():
    adapter = OpenAICompatibility()
    request = adapter.request({"model": "sonder", "input": [{
        "role": "user", "content": [{"type": "input_text", "text": "Hello"}]
    }]}, operation="responses")
    assert request.messages == ({"role": "user", "content": "Hello"},)
    response = adapter.response({"id": "resp-1", "model": "sonder", "output": [{
        "type": "message", "content": [{"type": "output_text", "text": "Hi"}]
    }]}, operation="responses")
    rendered = adapter.render(response)
    assert rendered["object"] == "response"
    assert rendered["output_text"] == "Hi"


def test_chat_response_normalizes_and_renders_without_losing_usage():
    adapter = OpenAICompatibility()
    response = adapter.response({"id": "chat-1", "model": "sonder", "choices": [{
        "message": {"role": "assistant", "content": "Done"},
        "finish_reason": "stop",
    }], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}, operation="chat.completions")
    assert adapter.render(response)["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}


def test_unsupported_shapes_are_rejected_and_policy_can_deny():
    denied = OpenAICompatibility(policy_hook=lambda *_: False)
    try:
        denied.request({"model": "m", "messages": [{"role": "user", "content": "x"}]},
                       operation="chat.completions")
    except CompatibilityError as exc:
        assert "policy" in str(exc)
    else:
        raise AssertionError("policy denial was ignored")
    with __import__("pytest").raises(CompatibilityError):
        OpenAICompatibility().request({"model": "m", "messages": [{
            "role": "user", "content": [{"type": "image_url"}]
        }]}, operation="chat.completions")


def test_complete_is_typed_policy_first_and_maps_provider_response_once():
    events = []
    calls = []
    adapter = OpenAICompatibility(
        policy_hook=lambda operation, payload: calls.append((operation, payload["model"])) or True,
        event_hook=lambda event: events.append(event["kind"]),
    )
    result = adapter.complete(
        {"model": "m", "messages": [{"role": "user", "content": "hello"}]},
        operation="chat.completions",
        model_hook=lambda request: CanonicalResponse(
            operation=request.operation,
            response_id="chatcmpl-1",
            model=request.model,
            text="world",
        ),
    )
    assert result.text == "world"
    assert calls == [("chat.completions", "m")]
    assert events == ["openai.request.normalized", "openai.response.normalized"]


def test_complete_rejects_provider_response_for_the_wrong_operation():
    adapter = OpenAICompatibility()
    with __import__("pytest").raises(CompatibilityError, match="does not match"):
        adapter.complete(
            {"model": "m", "input": "hello"},
            operation="responses",
            model_hook=lambda request: CanonicalResponse(
                operation="chat.completions",
                response_id="chatcmpl-1",
                model=request.model,
                text="wrong",
            ),
        )
