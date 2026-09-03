"""Agent decision generation lives in the adapters layer; the root name is a delegate."""
import pytest

import server
from sonder_runtime.adapters import agent_decision_generation as generation
from sonder_runtime.adapters.model_transport import ModelCallError

LENGTH_DETAIL = 'Ollama returned no assistant content; metadata={"done_reason": "length"}'


def _gen_from(responses, prompts):
    def gen(prompt):
        prompts.append(prompt)
        return responses.pop(0)

    gen.last_response_meta = {}
    return gen


def test_root_constant_is_an_identity_preserving_alias():
    assert server._AGENT_DECISION_REPAIR_LIMIT is generation.DECISION_REPAIR_LIMIT
    assert generation.DECISION_REPAIR_LIMIT == 2


def test_valid_decisions_are_returned_without_repair():
    prompts = []
    gen = _gen_from(['{"tool": "status", "args": {}}'], prompts)
    decision, raw, error = generation.generate_decision(gen, "task", write_chunk_hint=100)
    assert decision == {"tool": "status", "args": {}}
    assert error is None
    assert raw.startswith("{")
    assert prompts == ["task"]


def test_invalid_shapes_are_repaired_within_the_limit():
    prompts = []
    gen = _gen_from(["not json", '{"reason": "no tool"}', '{"final": "done"}'], prompts)
    decision, _raw, error = generation.generate_decision(gen, "task", write_chunk_hint=100)
    assert decision == {"final": "done"}
    assert error is None
    assert len(prompts) == 3
    assert "HOST FORMAT REPAIR 1/2" in prompts[1]
    assert "HOST FORMAT REPAIR 2/2" in prompts[2]
    assert "Parser error:" in prompts[1]


def test_repair_exhaustion_returns_the_last_error():
    gen = _gen_from(["x", "y", "z"], [])
    decision, raw, error = generation.generate_decision(gen, "task", repair_limit=2, write_chunk_hint=100)
    assert decision is None
    assert raw == "z"
    assert isinstance(error, ValueError)


def test_transport_failures_are_returned_and_cancellation_propagates():
    def failing(_prompt):
        raise ModelCallError("http", "boom", status=500)

    failing.last_response_meta = {}
    decision, raw, error = generation.generate_decision(failing, "task", write_chunk_hint=100)
    assert decision is None
    assert raw == ""
    assert error.kind == "http"

    def cancelled(_prompt):
        raise ModelCallError("cancelled", "stop")

    cancelled.last_response_meta = {}
    with pytest.raises(ModelCallError):
        generation.generate_decision(cancelled, "task", write_chunk_hint=100)


def test_length_limited_responses_get_the_recovery_hint_with_the_injected_chunk_size():
    prompts = []

    def gen(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ModelCallError("empty_response", LENGTH_DETAIL)
        return '{"tool": "file_write", "args": {"path": "a"}}'

    gen.last_response_meta = {}
    decision, _raw, error = generation.generate_decision(gen, "task", write_chunk_hint=777)
    assert decision["tool"] == "file_write"
    assert error is None
    assert "HOST LENGTH RECOVERY" in prompts[1]
    assert "at most 777 characters" in prompts[1]


def test_root_delegate_uses_the_server_chunk_hint():
    prompts = []

    def gen(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ModelCallError("empty_response", LENGTH_DETAIL)
        return '{"final": "ok"}'

    gen.last_response_meta = {}
    decision, _raw, error = server._agent_generate_decision(gen, "task")
    assert decision == {"final": "ok"}
    assert error is None
    assert "at most %d characters" % server._CLOUD_AGENT_WRITE_CHUNK_HINT in prompts[1]
