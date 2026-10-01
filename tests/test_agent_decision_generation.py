"""Focused contract tests for the bounded agent decision repair adapter."""

from sonder_runtime.adapters import agent_decision_generation as generation
from sonder_runtime.adapters.model_transport import ModelCallError


LENGTH_DETAIL = 'metadata={"done_reason": "length"}'


def _queued(responses, prompts):
    def gen(prompt):
        prompts.append(prompt)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    gen.last_response_meta = {}
    return gen


def test_local_length_repair_uses_real_json_cap_and_separate_file_write_step():
    prompts = []
    gen = _queued(
        [ModelCallError("empty_response", LENGTH_DETAIL), '{"final":"ok"}'],
        prompts,
    )

    decision, _raw, error = generation.generate_decision(
        gen, "task", write_chunk_hint=24000, cloud=False
    )

    assert decision == {"final": "ok"}
    assert error is None
    assert (
        "reply with the JSON object only, at most 4000 characters; "
        "put long file bodies in a separate file_write step"
    ) in prompts[1]
    assert "24000 characters" not in prompts[1]


def test_cloud_length_repair_keeps_existing_configured_hint():
    prompts = []
    gen = _queued(
        [ModelCallError("empty_response", LENGTH_DETAIL), '{"final":"ok"}'],
        prompts,
    )

    decision, _raw, error = generation.generate_decision(
        gen, "task", write_chunk_hint=24000, cloud=True
    )

    assert decision == {"final": "ok"}
    assert error is None
    assert "at most 24000 characters" in prompts[1]


def test_repair_exhaustion_marks_step_as_skippable_for_task_owner():
    prompts = []
    gen = _queued(["not json", "still not json", "again not json"], prompts)

    decision, raw, error = generation.generate_decision(
        gen, "task", repair_limit=2, write_chunk_hint=24000
    )

    assert decision is None
    assert raw == "again not json"
    assert isinstance(error, generation.DecisionUnusable)
    assert isinstance(error, ValueError)
    assert error.skip_step is True
    assert error.require_final is False


def test_finalization_exhaustion_is_marked_but_not_a_tool_step():
    gen = _queued(["x", "y", "z"], [])

    decision, _raw, error = generation.generate_decision(
        gen,
        "finalize",
        repair_limit=2,
        require_final=True,
        write_chunk_hint=24000,
    )

    assert decision is None
    assert isinstance(error, generation.DecisionUnusable)
    assert error.require_final is True


def test_length_empty_response_is_repaired_from_provider_error_metadata():
    prompts = []

    def gen(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ModelCallError("empty_response", LENGTH_DETAIL)
        return '{"tool":"status","args":{}}'

    gen.last_response_meta = {}
    decision, _raw, error = generation.generate_decision(
        gen, "task", write_chunk_hint=24000, cloud=False
    )

    assert decision == {"tool": "status", "args": {}}
    assert error is None
    assert len(prompts) == 2


def test_non_length_empty_response_is_returned_without_repair():
    gen = _queued(
        [ModelCallError("empty_response", 'metadata={"done_reason": "stop"}')],
        [],
    )

    decision, raw, error = generation.generate_decision(
        gen, "task", write_chunk_hint=24000, cloud=False
    )

    assert decision is None
    assert raw == ""
    assert isinstance(error, ModelCallError)
    assert error.kind == "empty_response"


def test_length_empty_response_after_repair_limit_is_skippable():
    gen = _queued(
        [
            ModelCallError("empty_response", LENGTH_DETAIL),
            ModelCallError("empty_response", LENGTH_DETAIL),
            ModelCallError("empty_response", LENGTH_DETAIL),
        ],
        [],
    )

    decision, _raw, error = generation.generate_decision(
        gen, "task", write_chunk_hint=24000, cloud=False
    )

    assert decision is None
    assert isinstance(error, generation.DecisionUnusable)
    assert error.require_final is False


def test_recovery_skips_only_once_even_after_a_successful_intermediate_step():
    recovery = generation.DecisionRecovery()
    observations = []
    error = generation.DecisionUnusable("bad")

    assert generation.skip_unusable_step(error, observations, 3, recovery=recovery)
    assert observations == ["step 3: decision unusable, skipped"]
    assert not generation.skip_unusable_step(error, observations, 5, recovery=recovery)
    assert recovery.unusable_steps == 1


def test_recovery_never_skips_finalization_or_transport_errors():
    recovery = generation.DecisionRecovery()
    observations = []

    assert not recovery.skip(
        generation.DecisionUnusable("final", require_final=True), 1, observations
    )
    assert not recovery.skip(ModelCallError("http", "down"), 2, observations)
    assert observations == []


def test_fake_gateway_empty_length_crosses_bridge_and_gets_local_repair():
    from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway
    from sonder_runtime.application.chat import provider_bridge
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.model_gateway import ModelResponse

    prompts = []
    class Gateway:
        def generate(self, request, _context):
            prompts.append(request.prompt)
            response = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
            if len(prompts) > 1:
                response = {"choices": [{"message": {"content": '{"final":"ok"}'}, "finish_reason": "stop"}]}
            return ModelResponse(OpenAICompatibleGateway._extract_text(response), "fake", request.tier)
    gateway = Gateway()

    def gen(prompt):
        with provider_bridge.bind_rung("sonder_inference", "code"):
            shaped, _ = provider_bridge.generate_via_gateway(
                gateway, {"messages": [{"role": "user", "content": prompt}]}, tier="code",
                context=local_owner_context(correlation_id="repair", source="test"),
            )
        return shaped["message"]["content"]
    decision, _, error = generation.generate_decision(gen, "task", write_chunk_hint=24000, cloud=False)
    assert decision == {"final": "ok"}
    assert error is None
    assert len(prompts) == 2
    assert "at most 4000 characters" in prompts[1]
