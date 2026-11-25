"""Native OpenAI tools, tool_choice, and tool_calls forwarding for Sonder Inference.

Exercises capability-gated native tool calling across ``SonderInferenceGateway``
and ``provider_bridge``:

* When a ``sonder-inference`` worker advertises native tool support (e.g.
  ``backends[].capabilities`` containing ``"tool_calling"`` from
  Sonder-Inference PR #96 ``c7ebf51``), ``tools``, ``tool_choice``, assistant
  ``tool_calls``, and ``role="tool"`` results forward natively (both
  non-streaming and SSE streaming) and return structured ``tool_calls``.
* When a worker does not advertise tool capability, requests fail closed before
  any POST (or reroute to local Ollama when ``SONDER_INFERENCE_FALLBACK=ollama``
  is configured).
"""
from __future__ import annotations

import json
import threading
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import legacy_chat_bridge
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    ENV_NATIVE_TOOLS,
    ENV_PRIVATE_WORKERS,
    MAX_RESPONSE_TOOL_CALLS,
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceResponse,
    native_tools_supported,
)
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

CALC_TOOL = {
    "type": "function",
    "function": {
        "name": "math_eval",
        "description": "Evaluate an arithmetic expression.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
}


def _health(
    *,
    status: str = "ready",
    backend: str = "llamaserver",
    capabilities: list[str] | None = None,
    models: tuple[str, ...] = ("qwen3.8:27b",),
    sonder_features: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    caps = ["streaming", "tool_calling"] if capabilities is None else list(capabilities)
    doc: dict[str, object] = {
        "status": status,
        "api_version": 1,
        "version": "0.1.0",
        "synthetic": False,
        "backends": [{"name": backend, "available": True, "capabilities": caps}],
        "models": [
            {"id": m, "backend": backend, "default": i == 0}
            for i, m in enumerate(models)
        ],
        "sonder": {
            "api_version": 1,
            "features": list(sonder_features) if sonder_features is not None else [],
        },
    }
    if extra:
        doc.update(extra)
    return doc


def _tool_call_reply(
    *,
    model: str = "qwen3.8:27b",
    content: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    finish_reason: str = "tool_calls",
) -> dict[str, object]:
    calls = (
        [
            {
                "id": "call_r1_0",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
            }
        ]
        if tool_calls is None
        else tool_calls
    )
    return {
        "id": "chatcmpl-r1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 24, "completion_tokens": 11, "total_tokens": 35},
        "timings": {"prompt_n": 24, "predicted_n": 11, "total_ms": 42.0},
        "sonder": {"api_version": 1, "request_id": "r1", "backend": "llamaserver"},
    }


def _text_reply(text: str = "It is 72F and sunny in Austin.", *, model: str = "qwen3.8:27b") -> dict[str, object]:
    return {
        "id": "chatcmpl-r2",
        "object": "chat.completion",
        "created": 2,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
        "timings": {"prompt_n": 40, "predicted_n": 10, "total_ms": 30.0},
        "sonder": {"api_version": 1, "request_id": "r2", "backend": "llamaserver"},
    }


class FakeInference:
    """In-memory GET/POST transport seams for SonderInferenceGateway."""

    def __init__(self, *, health=None, chat=None, get_error=None):
        self.health = _health() if health is None else health
        self.chat = chat if chat is not None else (lambda payload: _tool_call_reply())
        self.get_error = get_error
        self.gets: list[str] = []
        self.posts: list[dict[str, object]] = []

    def get(self, url, headers, timeout):
        self.gets.append(url)
        if self.get_error is not None:
            raise self.get_error
        if "/v1/sonder/health" in url:
            status = 200 if self.health.get("status") == "ready" else 503
            return status, json.dumps(self.health).encode("utf-8")
        return 404, b'{"error":{"code":"not_found"}}'

    def post(self, url, payload, headers, timeout):
        cloned = json.loads(json.dumps(payload))
        self.posts.append(cloned)
        result = self.chat(cloned)
        if isinstance(result, BaseException):
            raise result
        return result


def _gateway(fake: FakeInference, *, env=None, **settings) -> SonderInferenceGateway:
    return SonderInferenceGateway(
        SonderInferenceConfig(**settings),
        transport=fake.post,
        get_transport=fake.get,
        env={} if env is None else env,
        wall_clock=lambda: datetime(2026, 11, 25, tzinfo=timezone.utc),
    )


def _ctx():
    return local_owner_context(correlation_id="turn-tools-1", source="http", timeout_seconds=30.0)


@pytest.fixture(autouse=True)
def _restore_native_tools_probe():
    previous = bridge.set_native_tools_probe(None)
    try:
        yield
    finally:
        bridge.set_native_tools_probe(previous)


# -- 1. capability detection ---------------------------------------------------


@pytest.mark.parametrize("document", [
    _health(capabilities=["streaming", "tool_calling"]),
    _health(capabilities=["streaming", "tools"]),
    _health(capabilities=["streaming"], sonder_features=["tool_calling"]),
    _health(capabilities=["streaming"], sonder_features=["tools"]),
    _health(capabilities=["streaming"], extra={"features": ["native_tools"]}),
    _health(capabilities=["streaming"], extra={"capabilities": {"tool_calling": True}}),
    {
        "status": "ready",
        "api_version": 1,
        "backends": [{"name": "custom", "available": True, "capabilities": ["streaming"]}],
        "models": [{"id": "qwen3.8:27b", "backend": "custom", "default": True, "capabilities": ["tools"]}],
    },
])
def test_native_tools_supported_detects_advertised_markers(document):
    assert native_tools_supported(document, model="qwen3.8:27b", env={}) is True
    assert native_tools_supported(document, model="default", env={}) is True


@pytest.mark.parametrize("document", [
    None,
    {},
    _health(capabilities=["streaming", "structured_output"]),
    _health(backend="llamacpp", capabilities=["streaming", "prefix_reuse"]),
    {
        "status": "ready",
        "api_version": 1,
        "backends": [{"name": "llamaserver", "available": False, "capabilities": ["tool_calling"]}],
        "models": [{"id": "qwen3.8:27b", "backend": "llamaserver", "default": True}],
    },
    # upstream_capabilities on profiles is NOT a server-supported capability.
    _health(capabilities=["streaming"], extra={"upstream_capabilities": ["tools"]}),
])
def test_native_tools_supported_fails_closed_when_not_advertised(document):
    assert native_tools_supported(document, model="qwen3.8:27b", env={}) is False


def test_native_tools_supported_respects_per_model_backend_mapping():
    doc = {
        "status": "ready",
        "api_version": 1,
        "backends": [
            {"name": "llamaserver", "available": True, "capabilities": ["streaming", "tool_calling"]},
            {"name": "llamacpp", "available": True, "capabilities": ["streaming"]},
        ],
        "models": [
            {"id": "tool-model", "backend": "llamaserver", "default": True},
            {"id": "plain-model", "backend": "llamacpp", "default": False},
        ],
    }
    assert native_tools_supported(doc, model="tool-model", env={}) is True
    assert native_tools_supported(doc, model="default", env={}) is True
    assert native_tools_supported(doc, model="plain-model", env={}) is False


def test_native_tools_env_override_controls_detection_and_validates():
    no_tools = _health(capabilities=["streaming"])
    has_tools = _health(capabilities=["streaming", "tool_calling"])
    assert native_tools_supported(no_tools, env={ENV_NATIVE_TOOLS: "on"}) is True
    assert native_tools_supported(has_tools, env={ENV_NATIVE_TOOLS: "off"}) is False
    with pytest.raises(InvalidInput, match=ENV_NATIVE_TOOLS):
        native_tools_supported(has_tools, env={ENV_NATIVE_TOOLS: "maybe"})


def test_gateway_supports_native_tools_fails_closed_when_down():
    fake = FakeInference(get_error=urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    assert _gateway(fake).supports_native_tools() is False


# -- 2. gateway forwarding, validation & fail-closed refusal -------------------


@pytest.mark.parametrize("tool_choice", [
    "auto",
    "required",
    {"type": "function", "function": {"name": "get_weather"}},
])
def test_gateway_forwards_tools_and_tool_choice_and_extracts_tool_calls(tool_choice):
    fake = FakeInference(health=_health(capabilities=["streaming", "tool_calling"]))
    gateway = _gateway(fake)
    response = gateway.generate(
        ModelRequest(
            prompt="Check weather in Austin.",
            tier="code",
            options={"tools": [WEATHER_TOOL, CALC_TOOL], "tool_choice": tool_choice},
        ),
        _ctx(),
    )
    assert isinstance(response, SonderInferenceResponse)
    assert response.text == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (
        {
            "id": "call_r1_0",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
        },
    )
    assert len(fake.posts) == 1
    sent = fake.posts[0]
    assert sent["tools"] == [WEATHER_TOOL, CALC_TOOL]
    assert sent["tool_choice"] == tool_choice
    # last_response_meta stays content-free (count only, never raw arguments).
    meta = gateway.last_response_meta
    assert meta["finish_reason"] == "tool_calls"
    assert meta["tool_call_count"] == 1
    assert "Austin" not in json.dumps(meta)


def test_gateway_forwards_tool_choice_none_and_accepts_text_reply():
    fake = FakeInference(
        health=_health(capabilities=["streaming", "tool_calling"]),
        chat=lambda payload: _text_reply("No tools used."),
    )
    response = _gateway(fake).generate(
        ModelRequest(
            prompt="Just answer directly.",
            tier="code",
            options={"tools": [WEATHER_TOOL], "tool_choice": "none"},
        ),
        _ctx(),
    )
    assert isinstance(response, SonderInferenceResponse)
    assert response.text == "No tools used."
    assert response.tool_calls is None
    assert fake.posts[0]["tool_choice"] == "none"


def test_gateway_rejects_unsolicited_tool_calls_when_no_tools_or_tool_choice_none():
    # No tools offered on this request -> returned tool_calls must fail closed.
    fake_no_tools = FakeInference(chat=lambda payload: _tool_call_reply())
    with pytest.raises(DependencyUnavailable, match="unsolicited tool_calls"):
        _gateway(fake_no_tools).generate(
            ModelRequest(prompt="Plain text only.", tier="code"),
            _ctx(),
        )

    # Tools declared, but tool_choice="none" explicitly forbids tool calls.
    fake_forbidden = FakeInference(chat=lambda payload: _tool_call_reply())
    with pytest.raises(DependencyUnavailable, match="unsolicited tool_calls"):
        _gateway(fake_forbidden).generate(
            ModelRequest(
                prompt="Do not call tools.",
                tier="code",
                options={"tools": [WEATHER_TOOL], "tool_choice": "none"},
            ),
            _ctx(),
        )


def test_gateway_rejects_undeclared_tool_and_forced_tool_choice_mismatch():
    # Undeclared tool name in response.
    fake_undeclared = FakeInference(
        chat=lambda payload: _tool_call_reply(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "shell_exec", "arguments": "{}"},
            }],
        ),
    )
    with pytest.raises(DependencyUnavailable, match="undeclared tool"):
        _gateway(fake_undeclared).generate(
            ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )

    # Response calls another declared tool instead of the forced tool_choice function.
    fake_wrong_forced = FakeInference(
        chat=lambda payload: _tool_call_reply(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "math_eval", "arguments": '{"expression":"1+1"}'},
            }],
        ),
    )
    with pytest.raises(DependencyUnavailable, match="forced tool"):
        _gateway(fake_wrong_forced).generate(
            ModelRequest(
                prompt="Weather?",
                tier="code",
                options={
                    "tools": [WEATHER_TOOL, CALC_TOOL],
                    "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                },
            ),
            _ctx(),
        )


def test_gateway_caps_response_tool_calls_and_rejects_nan_mapping_arguments():
    too_many = [
        {
            "id": "call_%d" % i,
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
        }
        for i in range(MAX_RESPONSE_TOOL_CALLS + 1)
    ]
    fake_many = FakeInference(chat=lambda payload: _tool_call_reply(tool_calls=too_many))
    with pytest.raises(DependencyUnavailable, match="more than %d" % MAX_RESPONSE_TOOL_CALLS):
        _gateway(fake_many).generate(
            ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )

    # Mapping arguments containing NaN must fail closed (allow_nan=False).
    fake_nan = FakeInference(
        chat=lambda payload: _tool_call_reply(
            tool_calls=[{
                "id": "call_nan",
                "type": "function",
                "function": {"name": "get_weather", "arguments": {"temp": float("nan")}},
            }],
        ),
    )
    with pytest.raises(DependencyUnavailable, match="non-serializable"):
        _gateway(fake_nan).generate(
            ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )


def test_gateway_preserves_prose_alongside_tool_calls():
    fake = FakeInference(
        chat=lambda payload: _tool_call_reply(content="Checking weather now..."),
    )
    response = _gateway(fake).generate(
        ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
        _ctx(),
    )
    assert isinstance(response, SonderInferenceResponse)
    assert response.text == "Checking weather now..."
    assert response.tool_calls is not None and len(response.tool_calls) == 1


def test_gateway_refuses_tools_before_post_when_worker_does_not_advertise_capability():
    fake = FakeInference(health=_health(capabilities=["streaming"]))
    with pytest.raises(bridge.UnsupportedProviderFeature, match="tool_calling"):
        _gateway(fake).generate(
            ModelRequest(
                prompt="Weather in Austin?",
                tier="code",
                options={"tools": [WEATHER_TOOL], "tool_choice": "auto"},
            ),
            _ctx(),
        )
    assert len(fake.gets) == 1
    assert fake.posts == []


def test_gateway_refuses_tool_choice_only_when_worker_does_not_advertise_capability():
    fake = FakeInference(health=_health(capabilities=["streaming"]))
    with pytest.raises(bridge.UnsupportedProviderFeature, match="tool_calling"):
        _gateway(fake).generate(
            ModelRequest(prompt="hi", tier="code", options={"tool_choice": "none"}),
            _ctx(),
        )
    assert fake.posts == []


@pytest.mark.parametrize("bad_options", [
    {"tools": "not-a-list"},
    {"tools": [{"type": "function"}]},
    {"tools": [{"type": "other", "function": {"name": "x"}}]},
    {"tools": [{"type": "function", "function": {"name": "bad name!"}}]},
    {"tools": [WEATHER_TOOL, WEATHER_TOOL]},
    {"tools": [{"type": "function", "function": {"name": "x", "parameters": "bad"}}]},
    {"tool_choice": "required"},
    {"tool_choice": "invalid_mode"},
    {"tools": [WEATHER_TOOL], "tool_choice": {"type": "function", "function": {"name": "missing"}}},
    {"tool_choice": 42},
])
def test_gateway_rejects_malformed_tools_or_tool_choice_before_any_io(bad_options):
    fake = FakeInference()
    with pytest.raises(InvalidInput):
        _gateway(fake).generate(
            ModelRequest(prompt="hi", tier="code", options=bad_options),
            _ctx(),
        )
    assert fake.gets == [] and fake.posts == []


@pytest.mark.parametrize("bad_tool_calls", [
    "not-a-list",
    [{"id": "c1", "type": "other", "function": {"name": "x", "arguments": "{}"}}],
    [{"id": "c1", "type": "function", "function": {"name": "", "arguments": "{}"}}],
    [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": 123}}],
])
def test_gateway_rejects_malformed_response_tool_calls(bad_tool_calls):
    fake = FakeInference(chat=lambda payload: _tool_call_reply(tool_calls=bad_tool_calls))
    with pytest.raises(DependencyUnavailable, match="tool"):
        _gateway(fake).generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )


def test_gateway_still_rejects_empty_text_when_no_tool_calls_returned():
    fake = FakeInference(
        chat=lambda payload: {
            "id": "c1",
            "object": "chat.completion",
            "model": "qwen3.8:27b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 0},
            "sonder": {"api_version": 1},
        },
    )
    with pytest.raises(DependencyUnavailable, match="no usable text"):
        _gateway(fake).generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )


# -- 3. multi-turn tool history & trailing role="tool" -------------------------


def test_gateway_forwards_multiturn_assistant_tool_calls_and_trailing_tool_results():
    fake = FakeInference(chat=lambda payload: _text_reply("Austin is 72F and 2+2=4."))
    gateway = _gateway(fake)
    request = ModelRequest(
        prompt="",
        tier="code",
        system="Use tools when helpful.",
        history=(
            {"role": "user", "content": "Weather in Austin and 2+2?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": {"city": "Austin"}},
                    },
                    {
                        # Ollama-style call without id/type and with dict arguments.
                        "function": {"name": "math_eval", "arguments": {"expression": "2+2"}},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": '{"temp_f":72}'},
            {"role": "tool", "tool_name": "math_eval", "content": "4"},
        ),
        options={"tools": [WEATHER_TOOL, CALC_TOOL]},
    )
    response = gateway.generate(request, _ctx())
    assert response.text == "Austin is 72F and 2+2=4."
    sent = fake.posts[0]
    assert sent["messages"] == [
        {"role": "system", "content": "Use tools when helpful."},
        {"role": "user", "content": "Weather in Austin and 2+2?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
                },
                {
                    "id": "call_hist_1_1",
                    "type": "function",
                    "function": {"name": "math_eval", "arguments": '{"expression":"2+2"}'},
                },
            ],
        },
        {"role": "tool", "content": '{"temp_f":72}', "tool_call_id": "call_1", "name": "get_weather"},
        {"role": "tool", "content": "4", "name": "math_eval"},
    ]


def test_gateway_refuses_tool_history_when_worker_does_not_advertise_capability():
    fake = FakeInference(health=_health(capabilities=["streaming"]))
    request = ModelRequest(
        prompt="",
        tier="code",
        history=(
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "72F"},
        ),
    )
    with pytest.raises(bridge.UnsupportedProviderFeature):
        _gateway(fake).generate(request, _ctx())
    assert fake.posts == []


# -- 4. streaming delta.tool_calls over real loopback HTTP ---------------------


def test_streaming_tool_calls_round_trip_through_gateway_and_provider_bridge():
    received_payloads: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps(_health(capabilities=["streaming", "tool_calling"])).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received_payloads.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            chunks = [
                {
                    "id": "chatcmpl-s1",
                    "object": "chat.completion.chunk",
                    "model": "qwen3.8:27b",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                },
                {
                    "id": "chatcmpl-s1",
                    "object": "chat.completion.chunk",
                    "model": "qwen3.8:27b",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_stream_0",
                                        "type": "function",
                                        "function": {"name": "get_weather", "arguments": '{"city":'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-s1",
                    "object": "chat.completion.chunk",
                    "model": "qwen3.8:27b",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"Austin"}'},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-s1",
                    "object": "chat.completion.chunk",
                    "model": "qwen3.8:27b",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 18, "completion_tokens": 9, "total_tokens": 27},
                    "timings": {"prompt_n": 18, "predicted_n": 9, "total_ms": 25.0},
                    "sonder": {"api_version": 1, "request_id": "s1", "backend": "llamaserver"},
                },
            ]
            for chunk in chunks:
                self.wfile.write(("data: %s\n\n" % json.dumps(chunk)).encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = "http://127.0.0.1:%d" % httpd.server_address[1]
        gateway = SonderInferenceGateway(SonderInferenceConfig(base_url=base_url), env={})
        tool_deltas: list[dict[str, object]] = []
        live = stream_sink.LiveTurnStream(
            lambda text: True,
            write_tool_call=lambda delta: (tool_deltas.append(delta), True)[1],
        )
        ollama_payload = {
            "model": "ignored",
            "messages": [{"role": "user", "content": "Weather in Austin?"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "required",
            "options": {"temperature": 0.1},
        }
        with stream_sink.armed(live), bridge.bind_rung("sonder_inference", "code"):
            shaped, response = bridge.generate_via_gateway(
                gateway, ollama_payload, tier="code", context=_ctx(),
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert len(received_payloads) == 1
    assert received_payloads[0]["stream"] is True
    assert received_payloads[0]["tools"] == [WEATHER_TOOL]
    assert received_payloads[0]["tool_choice"] == "required"
    assert len(tool_deltas) == 2
    assert live.tool_call_deltas_forwarded == 2
    assert isinstance(response, SonderInferenceResponse)
    assert response.text == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == (
        {
            "id": "call_stream_0",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
        },
    )
    assert shaped["done_reason"] == "tool_calls"
    assert shaped["message"]["tool_calls"] == [
        {
            "id": "call_stream_0",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Austin"}'},
        }
    ]
    assert shaped["prompt_eval_count"] == 18
    assert shaped["eval_count"] == 9


# -- 5. private-worker placement with heterogeneous tool capability ------------


def test_private_worker_placement_routes_native_tools_only_to_capable_workers(tmp_path):
    primary = "http://127.0.0.1:11437"
    worker = "https://192.0.2.2:8443/sonder-inference"
    ca_file = tmp_path / "private-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")

    health_by_host = {
        primary: _health(backend="llamacpp", capabilities=["streaming"], models=("qwen3.8:27b",)),
        worker: _health(backend="llamaserver", capabilities=["streaming", "tool_calling"], models=("qwen3.8:27b",)),
    }
    posts: list[tuple[str, dict[str, object]]] = []

    def get_transport(url, headers, timeout):
        host = worker if url.startswith(worker) else primary
        return 200, json.dumps(health_by_host[host]).encode("utf-8")

    def post_transport(url, payload, headers, timeout):
        host = worker if url.startswith(worker) else primary
        posts.append((host, json.loads(json.dumps(payload))))
        return _tool_call_reply(model=payload["model"])

    env = {
        "SONDER_INFERENCE_BASE_URL": primary,
        "SONDER_INFERENCE_MODEL": "qwen3.8:27b",
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: json.dumps([
            {"url": worker, "ca_bundle": str(ca_file), "token_env": "NODE1_TOKEN"},
        ]),
        "NODE1_TOKEN": "t" * 32,
    }
    gateway = SonderInferenceGateway(
        transport=post_transport,
        get_transport=get_transport,
        env=env,
        wall_clock=lambda: datetime(2026, 11, 25, tzinfo=timezone.utc),
    )

    assert gateway.supports_native_tools(model="qwen3.8:27b") is True
    response = gateway.generate(
        ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
        _ctx(),
    )
    assert isinstance(response, SonderInferenceResponse)
    assert response.endpoint == "https://192.0.2.2:8443"
    assert [host for host, _ in posts] == [worker]

    # When neither primary nor worker advertises tool capability, fail closed with 0 POSTs.
    posts.clear()
    health_by_host[worker] = _health(backend="llamacpp", capabilities=["streaming"], models=("qwen3.8:27b",))
    gateway_no_tools = SonderInferenceGateway(
        transport=post_transport,
        get_transport=get_transport,
        env=env,
        wall_clock=lambda: datetime(2026, 11, 25, tzinfo=timezone.utc),
    )
    assert gateway_no_tools.supports_native_tools(model="qwen3.8:27b") is False
    with pytest.raises(bridge.UnsupportedProviderFeature):
        gateway_no_tools.generate(
            ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )
    assert posts == []


# -- 6. provider_bridge + PreSendFallbackGateway + Ollama fallback -------------


def test_provider_bridge_round_trips_native_tools_through_dispatch_and_fallback_wrappers():
    fake = FakeInference(health=_health(capabilities=["streaming", "tool_calling"]))
    inf_gw = _gateway(fake)

    class UnusedOllama:
        def generate(self, request, context):
            pytest.fail("Ollama fallback must not run when sonder-inference supports native tools")

        def embed(self, texts, context):
            pytest.fail("unexpected embed")

    fallback_gw = PreSendFallbackGateway(primary=inf_gw, fallback=UnusedOllama())
    dispatch_gw = ProviderDispatchGateway(
        providers={"sonder_inference": fallback_gw, "ollama": UnusedOllama()},
        tier_providers={"code": "sonder_inference"},
        default_generation_provider="sonder_inference",
        embedding_provider="ollama",
    )

    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Check Austin weather."},
        ],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
        "options": {"temperature": 0.2},
    }
    with bridge.bind_rung("sonder_inference", "code"):
        shaped, response = bridge.generate_via_gateway(
            dispatch_gw, payload, tier="code", context=_ctx(),
        )
    assert isinstance(response, SonderInferenceResponse)
    assert shaped["done_reason"] == "tool_calls"
    assert shaped["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert fake.posts[0]["tools"] == [WEATHER_TOOL]
    assert fake.posts[0]["tool_choice"] == "auto"


def test_provider_bridge_fails_closed_when_sonder_inference_lacks_tool_capability():
    fake = FakeInference(health=_health(capabilities=["streaming"]))
    inf_gw = _gateway(fake)
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Check Austin weather."}],
        "tools": [WEATHER_TOOL],
    }
    with bridge.bind_rung("sonder_inference", "code"):
        with pytest.raises(bridge.UnsupportedProviderFeature):
            bridge.generate_via_gateway(inf_gw, payload, tier="code", context=_ctx())
    assert fake.posts == []


def test_provider_bridge_never_enables_native_tools_for_other_providers_or_images():
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [WEATHER_TOOL],
    }
    assert bridge.ollama_only_feature(payload, provider="openrouter", native_tools=True)[0] == "tools"
    assert bridge.ollama_only_feature(payload, provider="openai_compatible", native_tools=True)[0] == "tools"
    assert bridge.ollama_only_feature(payload, provider="sonder_inference") == (
        "tools",
        "native tool calls are only available on Ollama tiers",
    )
    assert bridge.ollama_only_feature(payload, provider="sonder_inference", native_tools=True) is None
    # Images and format remain Ollama-only even when native tools are enabled.
    img_payload = {
        "messages": [{"role": "user", "content": "hi", "images": ["aGk="]}],
        "tools": [WEATHER_TOOL],
    }
    assert bridge.ollama_only_feature(img_payload, provider="sonder_inference", native_tools=True)[0] == "images"
    fmt_payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "format": {"type": "object"},
        "tools": [WEATHER_TOOL],
    }
    assert bridge.ollama_only_feature(fmt_payload, provider="sonder_inference", native_tools=True)[0] == "format"


def test_provider_bridge_refuses_and_reroutes_tool_role_messages_without_native_tools():
    # Mid-history or trailing role="tool" without tools/tool_choice/assistant.tool_calls
    # must still trigger ollama_only_feature and refuse fail-closed (or reroute to Ollama).
    mid_tool_payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "72F"},
            {"role": "user", "content": "summarize"},
        ],
    }
    trailing_tool_payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "72F"},
        ],
    }
    for payload in (mid_tool_payload, trailing_tool_payload):
        found = bridge.ollama_only_feature(payload, provider="sonder_inference")
        assert found == ("tool_calls", "images and tool calls are only available on Ollama tiers")
        assert bridge.ollama_only_feature(payload, provider="openrouter", native_tools=True) == found
        with pytest.raises(bridge.UnsupportedProviderFeature):
            bridge.model_request_from_ollama_payload(payload, tier="code", provider="sonder_inference")
        # Permitted only when sonder_inference positively advertises native tools.
        assert bridge.ollama_only_feature(payload, provider="sonder_inference", native_tools=True) is None
        req = bridge.model_request_from_ollama_payload(
            payload, tier="code", provider="sonder_inference", native_tools=True,
        )
        assert any(
            isinstance(entry, dict) and entry.get("role") == "tool" and entry.get("tool_call_id") == "call_1"
            for entry in req.history
        )

    # Also verify legacy_chat_bridge.ollama_only_reroute reroutes standalone role="tool" to Ollama.
    bindings = ProviderBindings(
        default_generation_provider="sonder_inference",
        tier_providers={t: "sonder_inference" for t in ("fast", "general", "code", "reasoning", "vision")},
        embedding_provider="ollama",
        fallbacks={"sonder_inference": "ollama"},
    )
    legacy_gw = _gateway(FakeInference(health=_health(capabilities=["streaming"])))
    graph = SimpleNamespace(provider_bindings=bindings, model_gateway=legacy_gw)
    rung = bridge.RungBinding(provider="sonder_inference", tier="code")
    bridge.set_native_tools_probe(lambda p: p == "sonder_inference" and legacy_gw.supports_native_tools())
    rerouted = legacy_chat_bridge.ollama_only_reroute(
        legacy_gw, trailing_tool_payload, rung, model="qwen2.5-coder:7b", graph=graph,
        loopback_endpoint=True, context_probe=lambda: 8192,
    )
    assert rerouted is not None


def test_ollama_only_reroute_respects_native_tools_probe_and_preserves_fallback():
    bindings_with_fallback = ProviderBindings(
        default_generation_provider="sonder_inference",
        tier_providers={t: "sonder_inference" for t in ("fast", "general", "code", "reasoning", "vision")},
        embedding_provider="ollama",
        fallbacks={"sonder_inference": "ollama"},
    )
    capable_gw = _gateway(FakeInference(health=_health(capabilities=["streaming", "tool_calling"])))
    legacy_gw = _gateway(FakeInference(health=_health(capabilities=["streaming"])))
    graph_capable = SimpleNamespace(provider_bindings=bindings_with_fallback, model_gateway=capable_gw)
    graph_legacy = SimpleNamespace(provider_bindings=bindings_with_fallback, model_gateway=legacy_gw)
    rung = bridge.RungBinding(provider="sonder_inference", tier="code")
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [{"role": "user", "content": "Check Austin weather."}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
        "options": {"num_predict": 64},
    }

    # Capable worker: probe reports True -> stays on sonder_inference (no Ollama reroute).
    bridge.set_native_tools_probe(lambda p: p == "sonder_inference" and capable_gw.supports_native_tools())
    assert legacy_chat_bridge.ollama_only_reroute(
        capable_gw, payload, rung, model="qwen2.5-coder:7b", graph=graph_capable,
        loopback_endpoint=True, context_probe=lambda: 16384,
    ) is None

    # Non-capable worker: probe reports False -> reroutes cleanly to local Ollama fallback.
    bridge.set_native_tools_probe(lambda p: p == "sonder_inference" and legacy_gw.supports_native_tools())
    with bridge.degradation_scope() as notes:
        rerouted = legacy_chat_bridge.ollama_only_reroute(
            legacy_gw, payload, rung, model="qwen2.5-coder:7b", graph=graph_legacy,
            loopback_endpoint=True, context_probe=lambda: 16384,
        )
    assert rerouted is not None
    assert rerouted["tools"] == [WEATHER_TOOL]
    assert rerouted["options"]["num_ctx"] == 16384
    assert notes == ["served by ollama (ollama-only feature: tools)"]


def test_presend_fallback_triggers_on_unreachable_and_does_not_mask_capability_refusal():
    ollama_calls: list[ModelRequest] = []

    class FakeOllama:
        def generate(self, request, context):
            ollama_calls.append(request)
            return ModelResponse(text="ollama reply", model="qwen2.5:7b", tier=request.tier)

        def embed(self, texts, context):
            return ()

    # Down primary -> SonderInferenceUnreachable -> falls back to local Ollama.
    down_inf = _gateway(
        FakeInference(get_error=urllib.error.URLError(ConnectionRefusedError(111, "refused"))),
    )
    gw_down = PreSendFallbackGateway(primary=down_inf, fallback=FakeOllama())
    req = ModelRequest(prompt="Weather?", tier="code", options={"tools": [WEATHER_TOOL]})
    res = gw_down.generate(req, _ctx())
    assert res.text == "ollama reply"
    assert len(ollama_calls) == 1

    # Ready primary without tool capability -> UnsupportedProviderFeature (not retried on fallback).
    ollama_calls.clear()
    no_tools_inf = _gateway(FakeInference(health=_health(capabilities=["streaming"])))
    gw_no_tools = PreSendFallbackGateway(primary=no_tools_inf, fallback=FakeOllama())
    with pytest.raises(bridge.UnsupportedProviderFeature):
        gw_no_tools.generate(req, _ctx())
    assert ollama_calls == []
