from sonder_runtime.application.protocol.openai_compatibility import (
    CanonicalResponse,
    CompatibilityError,
    OpenAICompatibility,
)


def _payload():
    return {"model": "local", "messages": [{"role": "user", "content": "hello"}]}


def test_complete_denies_before_injected_model_and_event_recording():
    calls = []
    compatibility = OpenAICompatibility(
        policy_hook=lambda operation, payload: calls.append(("policy", operation)) or False,
        event_hook=lambda event: calls.append(("event", event)),
    )

    def model(_request):
        calls.append(("model",))
        return CanonicalResponse("chat.completions", "id", "local", "answer")

    try:
        compatibility.complete(_payload(), operation="chat.completions", model_hook=model)
    except CompatibilityError as exc:
        assert "policy" in str(exc)
    else:
        raise AssertionError("policy denial was ignored")

    assert calls == [("policy", "chat.completions")]


def test_complete_records_normalized_request_and_response_events():
    events = []
    compatibility = OpenAICompatibility(event_hook=events.append)

    response = compatibility.complete(
        _payload(),
        operation="chat.completions",
        model_hook=lambda request: CanonicalResponse(
            request.operation, "chat-1", request.model, "answer"
        ),
    )

    assert response.text == "answer"
    assert [event["kind"] for event in events] == [
        "openai.request.normalized",
        "openai.response.normalized",
    ]
    assert events[0] == {
        "kind": "openai.request.normalized",
        "operation": "chat.completions",
        "model": "local",
        "stream": False,
    }
    assert events[1] == {
        "kind": "openai.response.normalized",
        "operation": "chat.completions",
        "model": "local",
        "response_id": "chat-1",
    }
