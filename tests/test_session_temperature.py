"""Per-session sampling temperature: resolution order, /temp, the range rule,
the auto default on interactive chat, and where the value is shown."""
from __future__ import annotations

import inspect
import math

import pytest

import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters import chat_temperature
from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.session import temperature_settings
from sonder_runtime.application.session.temperature_settings import (
    SessionTemperatureStore,
    temp_command,
)
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.sampling_temperature import (
    check_temperature,
    parse_temperature_setting,
    resolve_temperature,
)
from sonder_runtime.platform.environment_options import local_model_options


@pytest.fixture(autouse=True)
def _clean_store():
    temperature_settings.STORE.clear()
    yield
    temperature_settings.STORE.clear()


# --- resolution order: request > session > lane > preset -------------------


def test_request_beats_session_lane_and_preset():
    resolved = resolve_temperature(request=0.1, session=0.9, lane=0.4)
    assert (resolved.value, resolved.source) == (0.1, "request")


def test_session_beats_lane():
    resolved = resolve_temperature(session=0.9, lane=0.4)
    assert (resolved.value, resolved.source) == (0.9, "session")


def test_lane_beats_preset():
    resolved = resolve_temperature(lane=0.4)
    assert (resolved.value, resolved.source) == (0.4, "lane")


def test_nothing_set_is_the_preset_and_sends_none():
    resolved = resolve_temperature()
    assert (resolved.value, resolved.source) == (None, "preset")
    assert resolved.describe() == "effective temperature=auto (source: preset)"


def test_zero_is_a_set_value_not_unset():
    # 0 is falsy; it must still win over the session and lane.
    resolved = resolve_temperature(request=0, session=0.9, lane=0.4)
    assert (resolved.value, resolved.source) == (0.0, "request")
    assert resolved.describe() == "effective temperature=0 (source: request)"


def test_describe_formats_the_value():
    assert resolve_temperature(session=0.7).describe() == (
        "effective temperature=0.7 (source: session)"
    )
    assert resolve_temperature(lane=1.25).describe() == (
        "effective temperature=1.25 (source: lane)"
    )


def test_a_bad_value_is_refused_not_skipped():
    with pytest.raises(InvalidInput):
        resolve_temperature(request=3.0, session=0.7)


# --- the range rule: finite, 0..2, nothing clamped ---------------------------


@pytest.mark.parametrize("value", [0, 0.0, 1, 0.05, 2, 2.0])
def test_in_range_values_pass(value):
    assert check_temperature(value) == float(value)


@pytest.mark.parametrize(
    "value",
    [-0.01, 2.01, -1, 3, math.nan, math.inf, -math.inf, True, False, "0.7", [0.7], 10 ** 400],
)
def test_out_of_range_or_wrong_type_is_refused(value):
    with pytest.raises(InvalidInput, match="from 0 to 2"):
        check_temperature(value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.7", 0.7), (" 1 ", 1.0), ("0", 0.0), ("2", 2.0), ("auto", None),
     ("AUTO", None), ("default", None), ("off", None)],
)
def test_parse_setting(text, expected):
    assert parse_temperature_setting(text) == expected


@pytest.mark.parametrize("text", ["", "warm", "2.5", "-0.1", "nan", "inf", "1e400"])
def test_parse_setting_refuses(text):
    with pytest.raises(InvalidInput):
        parse_temperature_setting(text)


# --- the per-session store --------------------------------------------------


def test_sessions_are_independent_and_auto_clears():
    store = SessionTemperatureStore()
    store.set("a", 0.3)
    store.set("b", 1.2)
    assert store.get("a") == 0.3
    assert store.get("b") == 1.2
    assert store.get("c") is None
    store.set("a", None)
    assert store.get("a") is None
    assert store.get("b") == 1.2


@pytest.mark.parametrize("session", ["", "  ", "none", "NONE", None])
def test_no_session_cannot_hold_a_value(session):
    store = SessionTemperatureStore()
    with pytest.raises(InvalidInput):
        store.set(session, 0.5)
    assert store.get(session) is None


def test_store_is_bounded_least_recently_used_first():
    store = SessionTemperatureStore(max_sessions=2)
    store.set("a", 0.1)
    store.set("b", 0.2)
    store.get("a")  # a is now the most recent
    store.set("c", 0.3)
    assert store.get("b") is None
    assert (store.get("a"), store.get("c")) == (0.1, 0.3)


def test_store_refuses_out_of_range():
    store = SessionTemperatureStore()
    with pytest.raises(InvalidInput):
        store.set("a", 2.5)
    assert store.get("a") is None


# --- /temp ------------------------------------------------------------------


def test_temp_command_show_set_show_clear():
    store = SessionTemperatureStore()
    assert temp_command("", "s1", store=store).startswith(
        "temperature: auto (source: preset"
    )
    assert temp_command("0.7", "s1", store=store) == "temperature: 0.7 for this session"
    assert temp_command("", "s1", store=store) == "temperature: 0.7 (source: session)"
    assert temp_command("", "s2", store=store).startswith("temperature: auto")
    assert temp_command("auto", "s1", store=store) == (
        "temperature: auto for this session (the model preset applies)"
    )
    assert store.get("s1") is None


@pytest.mark.parametrize("arg", ["2.5", "-1", "nan", "inf", "hot"])
def test_temp_command_refuses_and_keeps_the_old_value(arg):
    store = SessionTemperatureStore()
    store.set("s1", 0.4)
    reply = temp_command(arg, "s1", store=store)
    assert reply.startswith("refused /temp:")
    assert "usage: /temp" in reply
    assert store.get("s1") == 0.4


def test_temp_command_without_a_session_refuses_to_set():
    reply = temp_command("0.5", "none", store=SessionTemperatureStore())
    assert reply.startswith("refused /temp")


def test_control_command_handles_temp_per_session(monkeypatch):
    monkeypatch.setattr(server.command_catalog, "console_tools", lambda: {})
    assert server.control_command("/temp 1.1", session="thread-a") == (
        "temperature: 1.1 for this session"
    )
    assert temperature_settings.STORE.get("thread-a") == 1.1
    assert temperature_settings.STORE.get("thread-b") is None
    assert server.control_command("/temp", session="thread-a") == (
        "temperature: 1.1 (source: session)"
    )


def test_http_slash_temp_uses_the_storage_session():
    reply = ts._handle_slash(
        "/temp 0.35", context={"mode": "local-open", "authorized": True},
        storage_session="acct:thread-x",
    )
    assert reply == "temperature: 0.35 for this session"
    assert temperature_settings.STORE.get("acct:thread-x") == 0.35


# --- the MCP default is auto, numbers still work ------------------------------


@pytest.mark.parametrize("name", ["sonder", "_sonder_impl", "_sonder_impl_serialized"])
def test_mcp_sonder_temperature_defaults_to_auto(name):
    func = getattr(server, name)
    func = getattr(func, "__wrapped__", func)
    default = inspect.signature(func).parameters["temperature"].default
    assert default is None


def test_resolve_turn_temperature_reads_the_session_and_notes_activity():
    temperature_settings.STORE.set("s-act", 0.6)
    with activity_tracker.response_span("chat:test", "hello", surface="test"):
        resolved = chat_temperature.resolve_turn_temperature(session="s-act")
        footer = activity_tracker.format_response()
    assert (resolved.value, resolved.source) == (0.6, "session")
    assert "sampling: effective temperature=0.6 (source: session)" in footer


def test_a_request_number_beats_the_session_value():
    temperature_settings.STORE.set("s-req", 0.6)
    resolved = chat_temperature.resolve_turn_temperature(request=0.2, session="s-req")
    assert (resolved.value, resolved.source) == (0.2, "request")


def test_code_repair_keeps_its_low_temperature_on_auto():
    assert chat_temperature.repair_temperature(None) == 0.2
    assert chat_temperature.repair_temperature(0.9) == 0.9
    assert chat_temperature.repair_temperature(0.0) == 0.0


# --- "auto" sends no temperature to Ollama ----------------------------------


def test_local_options_omit_temperature_on_auto():
    options = local_model_options(None, 64, 4096, environ={})
    assert "temperature" not in options
    assert local_model_options(0.4, 64, 4096, environ={})["temperature"] == 0.4


@pytest.mark.parametrize(("options", "expected"), [({}, None), ({"temperature": 0.45}, 0.45)])
def test_ollama_gateway_passes_none_when_no_temperature_is_set(options, expected):
    # The direct gateway used to fall back to 0.2; with nothing set it now
    # hands the factory None, which _make_generate leaves off the payload.
    seen = []

    def resolve(tier, strict=None):
        return ModelTarget("m", False, "code", True, provider_id="ollama")

    def make_generate(model, system, temperature, num_predict, num_ctx, **_kwargs):
        seen.append(temperature)

        def gen(prompt, history=None):
            return "ok"

        gen.last_usage = {}
        gen.last_response_meta = {}
        return gen

    gateway = OllamaGateway(target_resolver=resolve, generate_factory=make_generate)
    gateway.generate(
        ModelRequest("hello", "code", options=options),
        local_owner_context(correlation_id="req_temp"),
    )
    assert seen == [expected]


# --- serve path: session over the operator env, request override over both ---


def _install_serve_fakes(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "control_command", lambda *a, **k: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target", lambda *_a: ("local:latest", False, False, "general"),
    )
    monkeypatch.setattr(server, "_build_system", lambda *a, **k: "sys")
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    monkeypatch.setattr(server, "_cache_model_revision", lambda _model: "")
    monkeypatch.setattr(server, "_resolve_project", lambda *_a: None)
    monkeypatch.setattr(server, "_capture_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_apply_code_gate",
        lambda response, interaction_id=None, regenerate=None: (response, None, False),
    )

    def make_generate(model, system, temperature, num_predict, num_ctx, **_kwargs):
        def generate(prompt, history=None):
            calls.append(temperature)
            return "answer"

        generate.last_usage = {}
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)
    return calls


def test_serve_turn_is_auto_by_default(monkeypatch):
    monkeypatch.delenv("SONDER_SERVE_TEMPERATURE", raising=False)
    calls = _install_serve_fakes(monkeypatch)
    server._answer_with_history_impl("hello", [], tier="general", session="t1")
    assert calls == [None]


def test_serve_turn_session_beats_the_operator_env(monkeypatch):
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0.3")
    calls = _install_serve_fakes(monkeypatch)
    server._answer_with_history_impl("hello", [], tier="general", session="t2")
    temperature_settings.STORE.set("t2", 1.4)
    server._answer_with_history_impl("hello", [], tier="general", session="t2")
    assert calls == [0.3, 1.4]


def test_serve_turn_forced_override_beats_the_session(monkeypatch):
    from sonder_runtime.interfaces.http.serve_policy import serve_temperature_override

    calls = _install_serve_fakes(monkeypatch)
    temperature_settings.STORE.set("t3", 1.4)
    with serve_temperature_override(0.05):
        server._answer_with_history_impl("hello", [], tier="general", session="t3")
    assert calls == [0.05]


# --- the GUI slider's chat-body field ----------------------------------------

from tests.test_serve_contract_consistency import _chat, _http_server  # noqa: E402


def _stored_values():
    return dict(temperature_settings.STORE._values)


def test_session_temperature_field_sets_and_clears_the_session(monkeypatch):
    with _http_server(monkeypatch, []) as port:
        status, _ = _chat(port, model="sonder", session="gui-1", session_temperature=0.8)
        assert status == 200
        stored = _stored_values()
        assert list(stored.values()) == [0.8]
        assert any("gui-1" in key for key in stored)
        status, _ = _chat(port, model="sonder", session="gui-1")  # absent: kept
        assert status == 200
        assert list(_stored_values().values()) == [0.8]
        status, _ = _chat(port, model="sonder", session="gui-1", session_temperature="auto")
        assert status == 200
        assert _stored_values() == {}


@pytest.mark.parametrize("value", [2.5, -0.1, "hot", True, [0.5], "nan"])
def test_bad_session_temperature_is_a_400_and_no_model_call(monkeypatch, value):
    answers = []
    with _http_server(monkeypatch, answers) as port:
        status, payload = _chat(port, model="sonder", session="gui-2", session_temperature=value)
    assert status == 400
    assert "session_temperature" in payload["error"]["message"]
    assert answers == []
    assert _stored_values() == {}


def test_session_temperature_without_a_session_is_a_400(monkeypatch):
    answers = []
    with _http_server(monkeypatch, answers) as port:
        status, payload = _chat(port, model="sonder", session_temperature=0.5)
    assert status == 400
    assert answers == []
