"""Focused tests for the root-free bounded HTTP model route family."""

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.interfaces.http.facades.model_request import (
    MAX_MESSAGES,
    ModelFacadeError,
    ModelRequestFacade,
)


class _Gateway:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append((request, context))
        return ModelResponse(
            text="answer",
            model="local-model",
            tier=request.tier,
            tokens_in=3,
            tokens_out=2,
        )


def _context():
    return local_owner_context(correlation_id="http-test", source="http")


def test_facade_classifies_only_chat_and_responses_routes():
    facade = ModelRequestFacade()
    assert facade.route("/v1/chat/completions/").operation == "chat.completions"
    assert facade.route("/v1/responses").operation == "responses"
    assert facade.route("/v1/models") is None


def test_facade_normalizes_to_existing_model_request_contract():
    events = []
    facade = ModelRequestFacade(event_hook=events.append)
    request = facade.normalize(
        "/v1/chat/completions",
        {
            "model": "code",
            "messages": [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "prior"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"},
            ],
        },
    )
    model_request = facade.to_model_request(request)
    assert model_request.prompt == "current"
    assert model_request.tier == "code"
    assert model_request.system == "be concise"
    assert model_request.history == (("user", "prior"), ("assistant", "old answer"))
    assert events[0]["kind"] == "openai.request.normalized"


def test_facade_invokes_injected_gateway_and_renders_both_protocols():
    gateway = _Gateway()
    facade = ModelRequestFacade()
    payload = {"model": "code", "input": "hello"}
    invocation = facade.invoke("/v1/responses", payload, gateway, _context())

    assert gateway.requests[0][0].prompt == "hello"
    assert invocation.render()["object"] == "response"
    assert invocation.render()["output_text"] == "answer"
    assert invocation.render()["usage"]["total_tokens"] == 5

    chat = facade.invoke(
        "/v1/chat/completions",
        {"model": "code", "messages": [{"role": "user", "content": "hi"}]},
        gateway,
        _context(),
    )
    assert chat.render()["object"] == "chat.completion"
    assert chat.render()["choices"][0]["message"]["content"] == "answer"


def test_facade_preserves_injected_policy_and_bounds():
    calls = []
    facade = ModelRequestFacade(
        policy_hook=lambda operation, payload: calls.append(operation) or False,
    )
    try:
        facade.normalize(
            "/v1/chat/completions",
            {"model": "code", "messages": [{"role": "user", "content": "hi"}]},
        )
    except ModelFacadeError as exc:
        assert "policy" in str(exc)
    else:
        raise AssertionError("policy rejection was not preserved")
    assert calls == ["chat.completions"]

    bounded = [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)
    try:
        ModelRequestFacade().normalize(
            "/v1/chat/completions", {"model": "code", "messages": bounded}
        )
    except ModelFacadeError as exc:
        assert "bound" in str(exc)
    else:
        raise AssertionError("message bound was not enforced")


def test_facade_has_no_legacy_root_or_importlib_bypass():
    source = open(
        "sonder_runtime/interfaces/http/facades/model_request.py",
        encoding="utf-8",
    ).read()
    assert "import server" not in source
    assert "importlib" not in source
