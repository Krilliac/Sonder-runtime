"""A caller-supplied per-request temperature outside [0, 2] is refused before
any model call, so it is never mistaken for a model failure and never steps
the default route up to a stronger tier."""
from __future__ import annotations

import pytest

import server
from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.request_temperature import validate_request_temperature
from tests.test_tier_escalation import CODE_PROMPT, _install_chat_fakes

VALID = [0, 0.0, 0.2, 1, 2, 2.0]
INVALID = [-0.1, -2, 2.0001, 5, 1e308, float("inf"), float("-inf"), float("nan"),
           True, False, "0.5", 10 ** 400]


class _Gateway:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("hello", "fixture", request.tier)


def _context():
    return local_owner_context(correlation_id="temp-range")


@pytest.mark.parametrize("value", VALID)
def test_validator_accepts_the_range(value):
    assert validate_request_temperature(value) == value


def test_validator_treats_none_as_unset():
    assert validate_request_temperature(None) is None


@pytest.mark.parametrize("value", INVALID)
def test_validator_refuses_and_names_the_value(value):
    with pytest.raises(InvalidInput) as raised:
        validate_request_temperature(value)
    message = str(raised.value)
    assert message.startswith("temperature must be a finite number from 0 to 2, got ")
    assert repr(value)[:70] in message


@pytest.mark.parametrize("value", VALID)
def test_chat_service_accepts_the_range(value):
    gateway = _Gateway()
    ChatService(gateway).complete(ChatCommand(content="hi", temperature=value), _context())
    assert [request.options["temperature"] for request in gateway.requests] == [value]


@pytest.mark.parametrize("value", INVALID)
def test_chat_service_refuses_before_any_gateway_call(value):
    gateway = _Gateway()
    with pytest.raises(InvalidInput, match=r"temperature must be a finite number from 0 to 2, got "):
        ChatService(gateway).complete(ChatCommand(content="hi", temperature=value), _context())
    assert gateway.requests == []


def _payload(value):
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"temperature": value},
    }


@pytest.mark.parametrize("value", [0, 0.2, 2.0])
def test_provider_bridge_forwards_valid_temperature(value):
    request = bridge.model_request_from_ollama_payload(_payload(value), tier="general")
    assert request.options["temperature"] == float(value)


@pytest.mark.parametrize("value", [-0.1, -2, 2.0001, 5, 1e308, float("inf"),
                                   float("-inf"), float("nan")])
def test_provider_bridge_refuses_out_of_range_temperature(value):
    with pytest.raises(InvalidInput, match="temperature must be a finite number from 0 to 2"):
        bridge.model_request_from_ollama_payload(_payload(value), tier="general")


def test_provider_bridge_still_skips_boolean_temperature():
    request = bridge.model_request_from_ollama_payload(_payload(True), tier="general")
    assert "temperature" not in request.options


@pytest.mark.parametrize("value", [-0.1, 2.0001, 1e308, float("inf"), float("nan")])
def test_default_route_refuses_without_a_model_call_or_escalation(monkeypatch, value):
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-code": "fine", "m-general": "fine"})
    with pytest.raises(InvalidInput, match="temperature must be a finite number from 0 to 2"):
        server._sonder_impl_serialized(CODE_PROMPT, session="none", temperature=value)
    assert calls == []


@pytest.mark.parametrize("value", [-0.1, 2.0001, 1e308, float("inf"), float("nan")])
def test_mcp_sonder_tool_returns_a_clear_error_without_a_model_call(monkeypatch, value):
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-code": "fine", "m-general": "fine"})
    out = server.sonder(CODE_PROMPT, session="none", temperature=value)
    assert out.startswith("ERROR: temperature must be a finite number from 0 to 2, got ")
    assert repr(value) in out
    assert calls == []


def test_mcp_sonder_tool_still_answers_at_the_range_edge(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-code": "fine"})
    out = server.sonder(CODE_PROMPT, session="none", temperature=2.0)
    assert "fine" in out
    assert [call["model"] for call in calls] == ["m-code"]


@pytest.mark.parametrize("value", [-1, 2.5, 1e308, float("inf"), float("nan")])
def test_make_generate_refuses_before_building_a_model_call(value):
    with pytest.raises(InvalidInput, match="temperature must be a finite number from 0 to 2"):
        server._make_generate("m-code", "", value, 64, 2048)


# --- every tool entry point, not only the default chat route ----------------

BAD_ENTRY = [5.0, -1.0, 1e308, float("nan"), float("inf"), True, "0.7"]
GOOD_ENTRY = [0, 0.7, 2.0]


def _fake_offload_transport(monkeypatch):
    """Fake the transport under offload; return the list of posted payloads."""
    posted = []

    def reply(payload, **kwargs):
        posted.append(payload)
        return {"model": payload.get("model", "m")}, "fine"

    def reply_cloud(payload, **kwargs):
        posted.append(payload)
        return {"model": payload.get("model", "m")}, "fine", payload.get("model", "m")

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda environ: True)
    monkeypatch.setattr(server, "_chat_request", reply)
    monkeypatch.setattr(server, "_chat_request_with_cloud_fallback", reply_cloud)
    return posted


def _error_text(out):
    assert out.startswith("ERROR"), out
    assert "temperature must be a finite number from 0 to 2" in out
    return out


@pytest.mark.parametrize("tier", ["fast", "cloud-code"])
@pytest.mark.parametrize("value", BAD_ENTRY)
def test_offload_without_learning_refuses_before_any_transport_call(monkeypatch, tier, value):
    posted = _fake_offload_transport(monkeypatch)
    _error_text(server.offload("hi", tier=tier, temperature=value, learn=False))
    assert posted == []


@pytest.mark.parametrize("tier", ["fast", "cloud-code"])
@pytest.mark.parametrize("value", GOOD_ENTRY)
def test_offload_without_learning_posts_a_valid_temperature(monkeypatch, tier, value):
    posted = _fake_offload_transport(monkeypatch)
    out = server.offload("hi", tier=tier, temperature=value, learn=False)
    assert "fine" in out
    assert len(posted) == 1
    assert posted[0]["options"]["temperature"] == value


@pytest.mark.parametrize("value", [5.0, float("nan"), True])
def test_offload_with_learning_refuses_before_any_model_call(monkeypatch, value):
    posted = _fake_offload_transport(monkeypatch)
    _error_text(server.offload("hi", tier="fast", temperature=value, learn=True))
    assert posted == []


@pytest.mark.parametrize("value", [5.0, float("nan"), True])
def test_extract_grounded_refuses_before_any_model_call(monkeypatch, value):
    posted = _fake_offload_transport(monkeypatch)
    schema = '{"type":"object","properties":{"name":{"type":"string"}}}'
    _error_text(server.extract_grounded("name: Ada", schema, temperature=value))
    assert posted == []


@pytest.mark.parametrize("value", [5.0, float("nan"), True])
def test_parallel_generate_run_refuses_before_any_model_call(monkeypatch, value):
    posted = _fake_offload_transport(monkeypatch)
    _error_text(server.parallel_generate_run("write f", temperature=value))
    assert posted == []


@pytest.mark.parametrize("value", [5, -1.0, float("nan")])
def test_agent_dispatch_offload_refuses_without_a_model_call(monkeypatch, value):
    posted = _fake_offload_transport(monkeypatch)
    monkeypatch.setattr(server, "_require_managed_agent_admission", lambda: None)
    out = server._agent_dispatch("offload", {"prompt": "hi", "temperature": value})
    _error_text(out)
    assert posted == []


@pytest.mark.parametrize("value", [5, -1.0, float("nan")])
def test_loop_dispatch_offload_refuses_without_a_model_call(monkeypatch, value):
    posted = _fake_offload_transport(monkeypatch)
    out = server._loop_dispatch({"type": "offload", "prompt": "hi", "temperature": value,
                                 "learn": False})
    assert "temperature must be a finite number from 0 to 2" in str(out)
    assert posted == []


def test_oversized_value_is_truncated_in_the_message():
    for value in (10 ** 400, "x" * 5000):
        with pytest.raises(InvalidInput) as raised:
            validate_request_temperature(value)
        message = str(raised.value)
        assert message.endswith("...")
        assert len(message) < 200
