"""Structured output on the normal generation path, with fake backends."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import (
    ModelRequest,
    ModelResponse,
)
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.structured_output import generate_structured
from sonder_runtime.domain.common.errors import InvalidInput

pytestmark = pytest.mark.unit

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
}


def _context():
    return local_owner_context(correlation_id="structured-test", timeout_seconds=30.0)


class FakeGateway:
    """Replies from a script, one entry per call, recording each request."""

    def __init__(self, replies, *, model="fake:1b"):
        self.replies = list(replies)
        self.requests = []
        self.model = model

    def generate(self, request, context):
        self.requests.append(request)
        text = self.replies.pop(0)
        return ModelResponse(
            text=text, model=self.model, tier=request.tier,
            tokens_in=10, tokens_out=5,
        )

    def embed(self, texts, context):  # pragma: no cover - unused by these tests
        raise NotImplementedError


def _request():
    return ModelRequest("what is 6*7?", "code", options={"temperature": 0.0})


def test_valid_first_time_makes_one_call_and_sends_the_schema():
    gateway = FakeGateway(['{"answer": 42}'])
    result = generate_structured(gateway, _request(), _context(), SCHEMA)
    assert result.ok
    assert result.value == {"answer": 42}
    assert len(gateway.requests) == 1
    assert gateway.requests[0].options["schema"] == SCHEMA
    assert gateway.requests[0].options["temperature"] == 0.0
    assert result.model == "fake:1b"
    assert (result.tokens_in, result.tokens_out) == (10, 5)
    assert len(result.attempts) == 1


def test_invalid_then_valid_retries_once_with_the_error_appended():
    gateway = FakeGateway(['{"answer": "forty-two"}', '{"answer": 42}'])
    result = generate_structured(gateway, _request(), _context(), SCHEMA)
    assert result.ok and result.value == {"answer": 42}
    assert len(gateway.requests) == 2
    retry = gateway.requests[1]
    assert [row["role"] for row in retry.history] == ["user", "assistant"]
    assert retry.history[0]["content"] == gateway.requests[0].prompt
    assert '{"answer": "forty-two"}' in retry.history[1]["content"]
    assert "$.answer" in retry.prompt
    assert retry.options["schema"] == SCHEMA
    assert (result.tokens_in, result.tokens_out) == (20, 10)
    assert len(result.attempts) == 2
    assert result.attempts[0].errors


def test_invalid_twice_returns_a_typed_failure_after_exactly_two_calls():
    gateway = FakeGateway(['{"answer": "x"}', "not json"])
    result = generate_structured(gateway, _request(), _context(), SCHEMA)
    assert not result.ok
    assert result.value is None
    assert result.failure.kind == "invalid_json"
    assert result.failure.errors
    assert len(gateway.requests) == 2
    assert len(result.attempts) == 2


def test_backend_that_ignores_the_schema_is_still_validated():
    # The fake never honours the schema option and answers with prose.
    gateway = FakeGateway(["The answer is 42.", '{"answer": 42}'])
    result = generate_structured(gateway, _request(), _context(), SCHEMA)
    assert result.ok and result.value == {"answer": 42}
    assert len(gateway.requests) == 2


def test_retries_are_configurable_and_zero_means_one_call():
    gateway = FakeGateway(["nope", '{"answer": 1}'])
    result = generate_structured(
        gateway, _request(), _context(), SCHEMA, retries=0,
    )
    assert not result.ok
    assert len(gateway.requests) == 1


def test_retry_after_existing_history_ending_in_assistant_alternates_roles():
    gateway = FakeGateway(["nope", '{"answer": 42}'])
    request = ModelRequest(
        "what is 6*7?", "code",
        history=(
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ),
    )
    result = generate_structured(gateway, request, _context(), SCHEMA)
    assert result.ok
    retry = gateway.requests[1]
    sequence = [row["role"] for row in retry.history] + ["user"]
    assert sequence == ["user", "assistant", "user", "assistant", "user"]
    assert all(a != b for a, b in zip(sequence, sequence[1:], strict=False))


@pytest.mark.parametrize("schema", [
    dict(SCHEMA, **{"if": {}}),
    {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
    {"type": "array", "items": {"format": "date"}},
    {"anyOf": [{"type": "string"}, {"allOf": []}]},
    {"type": "string", "pattern": "("},
])
def test_unsupported_schema_keywords_fail_before_any_model_call(schema):
    gateway = FakeGateway(['{"answer": 1}'])
    with pytest.raises(InvalidInput):
        generate_structured(gateway, _request(), _context(), schema)
    assert gateway.requests == []


def test_a_non_object_schema_is_rejected_before_any_call():
    gateway = FakeGateway([])
    with pytest.raises(InvalidInput):
        generate_structured(gateway, _request(), _context(), "not a schema")
    assert gateway.requests == []


def _ollama(factory):
    return OllamaGateway(
        target_resolver=lambda tier, strict=None: ModelTarget("m:1b", False, "code"),
        generate_factory=factory,
        session_num_ctx=2048,
    )


def test_ollama_gateway_forwards_the_schema_option_to_the_generator():
    seen = {}

    def factory(*args, **kwargs):
        seen.update(kwargs)

        def gen(prompt, history=None):
            return '{"answer": 1}'

        gen.last_usage = {"tokens_in": 3, "tokens_out": 2}
        return gen

    response = _ollama(factory).generate(
        ModelRequest("q", "code", system="s", options={"schema": SCHEMA}),
        _context(),
    )
    assert seen["schema"] == SCHEMA
    assert response.model == "m:1b"


def test_ollama_gateway_without_a_schema_option_adds_no_schema_kwarg():
    seen = {}

    def factory(*args, **kwargs):
        seen.update(kwargs)
        gen = lambda prompt, history=None: "ok"
        gen.last_usage = {}
        return gen

    _ollama(factory).generate(ModelRequest("q", "code", system="s"), _context())
    assert "schema" not in seen


def test_ollama_gateway_rejects_a_non_mapping_schema_option():
    with pytest.raises(InvalidInput):
        _ollama(lambda *a, **k: None).generate(
            ModelRequest("q", "code", system="s", options={"schema": "x"}),
            _context(),
        )


def test_retry_through_the_real_generator_sends_only_role_content_messages(monkeypatch):
    import server

    payloads = []
    replies = iter(['{"answer": "x"}', '{"answer": 42}'])

    def fake_chat(payload, **kwargs):
        payloads.append(payload)
        text = next(replies)
        return {"message": {"content": text}, "prompt_eval_count": 3, "eval_count": 2}, text

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    gateway = OllamaGateway(
        target_resolver=lambda tier, strict=None: ModelTarget("m:1b", False, "code"),
        generate_factory=server._make_generate,
        session_num_ctx=2048,
    )
    result = generate_structured(
        gateway, ModelRequest("what is 6*7?", "code", system="s"), _context(), SCHEMA,
    )
    assert result.ok and result.value == {"answer": 42}
    assert len(payloads) == 2
    assert payloads[1]["format"] == SCHEMA
    messages = payloads[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert all(isinstance(m, dict) and set(m) == {"role", "content"} for m in messages)


def test_structured_request_reaches_the_evidence_gateway_requiring_structured():
    from types import SimpleNamespace

    from sonder_runtime.application.routing.evidence_gateway import (
        CapabilityEvidenceGateway,
    )
    from sonder_runtime.domain.common.errors import DependencyUnavailable
    from sonder_runtime.domain.routing.backend_conformance import BackendCapability

    route = SimpleNamespace(provider_id="ollama", model="fake:1b", cloud=False)
    class Inner(FakeGateway):
        def resolve_route(self, request, context):
            return route

    inner = Inner(['{"answer": 1}'])
    gateway = CapabilityEvidenceGateway(
        inner, None, lambda *_: None, mode="strict",
    )
    with pytest.raises(DependencyUnavailable):
        generate_structured(gateway, _request(), _context(), SCHEMA)
    assert inner.requests == []

    from sonder_runtime.application.routing.request_capabilities import (
        request_requirements,
    )
    assert BackendCapability.STRUCTURED in request_requirements({"schema": SCHEMA})
    assert BackendCapability.STRUCTURED in request_requirements({"options": {"schema": SCHEMA}})
    assert not request_requirements({"options": {"temperature": 0.0}})
