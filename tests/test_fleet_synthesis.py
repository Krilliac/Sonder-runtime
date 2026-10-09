from sonder_runtime.adapters.fleet_synthesis import (
    SUMMARY_TRUNCATED_MARKER,
    append_truncation_marker,
    generate_synthesis,
    json_num_predict,
)
from sonder_runtime.application.chat import provider_bridge
import ast
import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import fleet_synthesis


@pytest.mark.parametrize("reason,expected", [("length", "length"), ("stop", "stop"), (None, None), ("unknown-private-text", None)])
def test_actual_inference_response_keeps_finish_evidence_across_provider_bridge(reason, expected):
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceResponse
    from sonder_runtime.application.chat.provider_bridge import ollama_shape
    response = SonderInferenceResponse(
        text="## Verified Findings (what", model="fake-model", tier="code", finish_reason=reason,
    )
    shaped = ollama_shape(response)
    assert shaped.get("done_reason") == expected
    rendered = append_truncation_marker(response.text, generator=shaped)
    assert rendered.endswith(SUMMARY_TRUNCATED_MARKER) is (reason == "length")


def _host_functions(names, scope):
    """Run production host functions with inert model/storage boundaries."""
    path = Path(__file__).resolve().parents[1] / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    return scope


@pytest.mark.parametrize("provider,enabled,budget", [("sonder_inference", True, 3448), (None, True, 1400), ("sonder_inference", False, 1400)])
@pytest.mark.parametrize("reason_key", ["done_reason", "finish_reason"])
def test_master_offload_applies_budget_and_marks_raw_provider_truncation(provider, enabled, budget, reason_key):
    requests = []
    raw = {reason_key: "length", "model": "test", "prompt_eval_count": 3, "eval_count": 330}
    def chat(payload, **kwargs):
        requests.append(payload)
        return raw, "## Verified Findings (what"
    scope = _host_functions({"_offload_tier_impl"}, dict(
        TIMEOUT=180, TIERS={"code": "test"}, os=os, contextlib=contextlib,
        _provider_bridge=provider_bridge,
        time=SimpleNamespace(time=lambda: 0), _fleet_synthesis=fleet_synthesis,
        _parse_schema_arg=lambda value: value, _refresh_live_cloud_tiers=lambda: None,
        _bound_request_timeout=lambda timeout, default: timeout,
        _bridge_provider_for_tier=lambda tier: provider,
        _is_cloud_tier=lambda *a: False, _auto_model_context=lambda model: 4096,
        _should_learn=lambda *a: False,
        _platform_local_model_options=lambda temperature, num_predict, num_ctx, **kw: {"num_predict": num_predict},
        context_policy=SimpleNamespace(native=lambda value: value),
        _keep_alive_for=lambda model: "5m", _chat_request=chat,
        _model_usage_count=lambda value: value, _model_usage_source=lambda *a: "provider",
        run_model_step=lambda call, **kwargs: call(), _legacy_model_failure_code=lambda e: "error",
        activity_tracker=SimpleNamespace(record_model_call=lambda **kwargs: None),
    ))
    result = scope["_offload_tier_impl"]("audit", tier="code", num_predict=1400, learn=False, fleet_synthesis=enabled)
    assert requests[0]["options"]["num_predict"] == budget
    assert result.endswith(SUMMARY_TRUNCATED_MARKER) is enabled
    assert "Verified Findings" in result


def test_durable_local_fanout_synthesis_marks_length_without_changing_its_budget():
    requests = []
    def post(endpoint, payload, **kwargs):
        requests.append(payload)
        return {"message": {"content": "partial"}, "done_reason": "length"}, 1
    scope = _host_functions({"_fanout_synthesis_generate"}, dict(
        _fleet_synthesis=fleet_synthesis, os=os,
        FANOUT_SYNTHESIS_NUM_PREDICT=1400, FANOUT_SYNTHESIS_TIMEOUT_SECONDS=90,
        _fanout_synthesis_prompt=lambda *a: ("audit", 4096),
        _platform_local_model_options=lambda temp, count, ctx, **kw: {"num_predict": count},
        context_policy=SimpleNamespace(native=lambda n: n), _keep_alive_for=lambda model: "5m",
        _post_model=post, _strip_inline_thinking=lambda text: text,
    ))
    result = scope["_fanout_synthesis_generate"]("test", {})
    assert result == "partial\n\n" + SUMMARY_TRUNCATED_MARKER
    assert requests[0]["options"]["num_predict"] == 1400


class FakeModel:
    def __init__(self, text, **metadata):
        self.text = text
        self.last_response_meta = metadata
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.text


class MetadataNamedModel:
    def __init__(self, text, **metadata):
        self.text = text
        self.response_metadata = metadata

    def __call__(self, prompt):
        del prompt
        return self.text


def test_bridged_budget_reserves_hidden_reasoning_without_changing_ollama():
    assert json_num_predict(1400) == 1400
    assert json_num_predict(2048, bridged=True) == 4096
    assert json_num_predict(15000, bridged=True) == 16384
    assert json_num_predict(20000, bridged=True) == 16384


def test_length_truncated_reply_gets_explicit_marker():
    model = FakeModel("## Verified Findings (what", done_reason="length")
    assert generate_synthesis(model, "audit") == (
        "## Verified Findings (what\n\n" + SUMMARY_TRUNCATED_MARKER
    )
    assert model.calls == ["audit"]


def test_finish_reason_alias_is_supported_without_inventing_metadata():
    model = FakeModel("partial", finish_reason="length")
    assert append_truncation_marker("partial", generator=model).endswith(
        SUMMARY_TRUNCATED_MARKER
    )


def test_wrapped_provider_metadata_is_supported():
    model = MetadataNamedModel("partial", finish_reason=" LENGTH ")
    assert append_truncation_marker("partial", generator=model).endswith(
        SUMMARY_TRUNCATED_MARKER
    )


def test_raw_provider_metadata_mapping_marks_truncated_body():
    # The non-learning offload boundary has the raw response fields available
    # before it reduces the response to text.  Keep this contract independent
    # of a live gateway so the production call site can pass that mapping here.
    assert append_truncation_marker(
        "partial", generator={"finish_reason": "length"}
    ).endswith(SUMMARY_TRUNCATED_MARKER)


def test_nontruncated_reply_is_unchanged():
    model = FakeModel("complete", done_reason="stop")
    assert generate_synthesis(model, "audit") == "complete"


def test_missing_or_unrelated_metadata_is_unchanged():
    assert append_truncation_marker("complete", generator=FakeModel("complete")) == "complete"
    assert append_truncation_marker("complete", generator=FakeModel("complete", done_reason="other")) == "complete"
