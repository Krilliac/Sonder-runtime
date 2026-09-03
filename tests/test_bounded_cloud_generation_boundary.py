"""Bounded hosted generation lives in the adapters layer; root names are aliases."""
import pytest

import server
from sonder_runtime.adapters import bounded_cloud_generation as bounded
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.context_formatting import rough_token_count


def _fake_gen(content="hello world", tokens_out=None, error=None):
    def gen(prompt, history=None):
        gen.calls.append((prompt, gen.num_predict_override))
        if error is not None:
            raise error
        gen.last_usage = {"tokens_out": tokens_out} if tokens_out is not None else {}
        gen.last_response_meta = {"done_reason": "stop"}
        return content

    gen.calls = []
    gen.num_predict_override = None
    gen.last_usage = {}
    gen.last_response_meta = {}
    return gen


def test_root_names_are_identity_preserving_aliases():
    assert server._CLOUD_AGENT_NUM_PREDICT is bounded.CLOUD_AGENT_NUM_PREDICT
    assert server._CLOUD_AGENT_OUTPUT_BUDGET is bounded.CLOUD_AGENT_OUTPUT_BUDGET
    assert server._bounded_cloud_agent_generate is bounded.bounded_cloud_generate
    assert (bounded.CLOUD_AGENT_NUM_PREDICT, bounded.CLOUD_AGENT_OUTPUT_BUDGET) == (16384, 65536)


def test_usage_is_charged_from_the_larger_of_reported_and_estimated():
    content = "x" * 400
    gen = _fake_gen(content, tokens_out=3)
    wrapped = bounded.bounded_cloud_generate(gen, per_call_limit=50, total_budget=1000)
    assert wrapped("p") == content
    assert wrapped.output_tokens_used == max(3, rough_token_count(content))
    assert gen.calls == [("p", 50)]
    assert gen.num_predict_override is None
    assert wrapped.last_usage == {"tokens_out": 3}
    assert wrapped.last_response_meta == {"done_reason": "stop"}
    assert wrapped.output_token_budget == 1000
    assert wrapped.output_budget_state["spent"] == wrapped.output_tokens_used


def test_exhausted_budget_refuses_before_calling_the_model():
    gen = _fake_gen("ok", tokens_out=10)
    wrapped = bounded.bounded_cloud_generate(gen, per_call_limit=10, total_budget=15)
    wrapped("a")
    wrapped("b")
    with pytest.raises(ModelCallError) as excinfo:
        wrapped("c")
    assert excinfo.value.kind == "budget"
    assert excinfo.value.attempts == 0
    assert excinfo.value.cloud is True
    assert [limit for _prompt, limit in gen.calls] == [10, 5]


def test_failed_calls_charge_the_full_ceiling_unless_nothing_was_attempted():
    failing = _fake_gen(error=ModelCallError("http", "boom", status=500, attempts=1))
    wrapped = bounded.bounded_cloud_generate(failing, per_call_limit=20, total_budget=100)
    with pytest.raises(ModelCallError):
        wrapped("a")
    assert wrapped.output_tokens_used == 20
    assert failing.num_predict_override is None
    unattempted = _fake_gen(error=ModelCallError("transport", "down", attempts=0))
    wrapped_unattempted = bounded.bounded_cloud_generate(unattempted, per_call_limit=20, total_budget=100)
    with pytest.raises(ModelCallError):
        wrapped_unattempted("a")
    assert wrapped_unattempted.output_tokens_used == 0


def test_shared_budget_state_is_honoured_and_updated():
    state = {"spent": 90, "total": 100}
    gen = _fake_gen("ok", tokens_out=1)
    wrapped = bounded.bounded_cloud_generate(gen, per_call_limit=50, total_budget=5000, budget_state=state)
    wrapped("a")
    assert gen.calls[0][1] == 10
    assert state["spent"] >= 91
    assert wrapped.output_token_budget == 100
    assert wrapped.output_budget_state is state
