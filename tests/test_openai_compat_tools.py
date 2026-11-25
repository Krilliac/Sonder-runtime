"""Unit and loopback-SSE tests for native OpenAI tools/tool_choice/tool_calls support.

Covers strict request validation, multi-turn tool-result history shaping,
non-streaming tool_calls parsing, streaming tool_call delta assembly (both
directly via ToolCallDeltaAssembler and end-to-end over real loopback HTTP SSE
with LiveTurnStream), content-free telemetry, and spend metering.
"""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from decimal import Decimal
from types import MappingProxyType

import pytest

from sonder_runtime.adapters.inference.anthropic_spend import SpendLedger
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    MAX_RESPONSE_TOOL_CALLS,
    MAX_TOOL_CALL_ARGUMENTS_CHARS,
    MAX_TOOL_CALL_ARGUMENTS_DEPTH,
    MAX_TOOL_CALL_ID_CHARS,
    MAX_TOOL_DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
    MAX_TOOL_SCHEMA_CHARS,
    MAX_TOOLS,
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    OpenAICompatibleResponse,
    OpenAIDailyCapReached,
    OpenAIToolCall,
    OpenAIToolTelemetry,
    ToolCallDeltaAssembler,
    normalize_openai_tool_choice,
    normalize_openai_tools,
    parse_openai_tool_calls,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain.chat_tools import CORE_CHAT_TOOL_DEFINITIONS
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DependencyUnavailable,
    InvalidInput,
)
from sonder_runtime.domain.openai_spend_policy import ChatPrice
from sonder_runtime.platform.metrics import default_registry

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up current weather by city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "units": {"type": "string", "enum": ["c", "f"]},
            },
            "required": ["city"],
        },
    },
}

MATH_TOOL = {
    "type": "function",
    "function": {
        "name": "math_eval",
        "description": "Evaluate a math expression.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "strict": True,
    },
}

ZERO_ARG_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return current UTC time.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _ctx(*, cloud: bool = False, timeout: float = 30.0):
    return local_owner_context(
        correlation_id="req_tools", cloud_allowed=cloud, timeout_seconds=timeout,
    )


def _local_cfg(**kw) -> OpenAICompatibleConfig:
    kw.setdefault("base_url", "http://127.0.0.1:18080")
    kw.setdefault("model", "bonsai-27b-pq2")
    return OpenAICompatibleConfig(**kw)


def _tool_response(
    tool_calls,
    *,
    content=None,
    finish_reason: str | None = "tool_calls",
    reasoning_content: str | None = None,
    usage: dict | None = None,
    timings: dict | None = None,
) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    doc: dict = {
        "id": "chatcmpl-tools-1",
        "object": "chat.completion",
        "model": "bonsai-27b-pq2",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        doc["usage"] = usage
    else:
        doc["usage"] = {"prompt_tokens": 90, "completion_tokens": 18, "total_tokens": 108}
    if timings is not None:
        doc["timings"] = timings
    return doc


@contextmanager
def _serve_sse(frames: list[str], *, capture: list[dict] | None = None):
    body = "".join(f"data: {frame}\n\n" for frame in frames).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if capture is not None:
                capture.append({
                    "path": self.path,
                    "payload": json.loads(raw.decode("utf-8")) if raw else {},
                    "accept": self.headers.get("Accept"),
                })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


# -- 1. Request shaping & strict validation -----------------------------------


def test_plain_request_omits_tool_fields_from_payload():
    seen: list[dict] = []

    def transport(url, payload, headers, timeout):
        seen.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    resp = gw.generate(ModelRequest(prompt="hello", tier="fast"), _ctx())

    assert isinstance(resp, ModelResponse)
    assert isinstance(resp, OpenAICompatibleResponse)
    assert resp.text == "ok"
    assert resp.tool_calls == ()
    assert resp.finish_reason == "stop"
    assert resp.tool_telemetry is None
    assert "tools" not in seen[0]
    assert "tool_choice" not in seen[0]
    assert "parallel_tool_calls" not in seen[0]


def test_request_forwards_normalized_tools_tool_choice_and_parallel_flag():
    seen: list[dict] = []
    mutable_tool = {
        "type": "function",
        "function": {
            "name": " get_weather ",
            "description": "Look up weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            "strict": False,
        },
    }

    def transport(url, payload, headers, timeout):
        seen.append(payload)
        return _tool_response([{
            "id": "call_w1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
        }])

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    resp = gw.generate(
        ModelRequest(
            prompt="Weather in Tokyo?",
            tier="code",
            options={
                "tools": [mutable_tool, MATH_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                "parallel_tool_calls": False,
            },
        ),
        _ctx(),
    )
    # Mutating caller schema after send must not affect normalized payload.
    mutable_tool["function"]["parameters"]["properties"]["extra"] = {"type": "string"}
    assert "extra" not in seen[0]["tools"][0]["function"]["parameters"]["properties"]
    assert seen[0]["tools"][0]["function"]["name"] == "get_weather"
    assert seen[0]["tools"][0]["function"]["strict"] is False
    assert seen[0]["tools"][1]["function"]["strict"] is True
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert seen[0]["parallel_tool_calls"] is False
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"


def test_core_chat_tool_definitions_validate_cleanly():
    raw = [defn.to_ollama_tool() for defn in CORE_CHAT_TOOL_DEFINITIONS[:8]]
    normalized, names = normalize_openai_tools(raw)
    assert len(normalized) == 8
    assert names == frozenset(defn.name for defn in CORE_CHAT_TOOL_DEFINITIONS[:8])


@pytest.mark.parametrize("bad_tools", [
    "not-a-list",
    {"type": "function"},
    123,
    [],
    ["not-a-dict"],
    [{"type": "code_interpreter", "function": {"name": "foo"}}],
    [{"type": "function", "function": {"name": "foo"}, "extra": 1}],
    [{"type": "function", "function": "not-a-dict"}],
    [{"type": "function", "function": {"name": "foo", "unknown_key": True}}],
    [{"type": "function", "function": {"name": ""}}],
    [{"type": "function", "function": {"name": "9starts_with_digit"}}],
    [{"type": "function", "function": {"name": "bad name"}}],
    [{"type": "function", "function": {"name": "a" * (MAX_TOOL_NAME_CHARS + 1)}}],
    [{"type": "function", "function": {"name": "dup"}}, {"type": "function", "function": {"name": "dup"}}],
    [{"type": "function", "function": {"name": "foo", "description": 42}}],
    [{"type": "function", "function": {"name": "foo", "description": "bad\x00nul"}}],
    [{"type": "function", "function": {"name": "foo", "description": "x" * (MAX_TOOL_DESCRIPTION_CHARS + 1)}}],
    [{"type": "function", "function": {"name": "foo", "parameters": ["not-an-object"]}}],
    [{"type": "function", "function": {"name": "foo", "parameters": {"type": "array"}}}],
    [{"type": "function", "function": {"name": "foo", "parameters": {"type": "object", "x": float("nan")}}}],
    [{"type": "function", "function": {"name": "foo", "strict": "yes"}}],
])
def test_invalid_tools_rejected_before_transport(bad_tools):
    called = []
    gw = OpenAICompatibleGateway(_local_cfg(), transport=lambda *a: called.append(a) or {})
    with pytest.raises(InvalidInput):
        gw.generate(ModelRequest(prompt="hi", tier="code", options={"tools": bad_tools}), _ctx())
    assert called == []


def test_tools_count_depth_and_schema_size_bounds_enforced():
    too_many = [
        {"type": "function", "function": {"name": f"tool_{i}"}}
        for i in range(MAX_TOOLS + 1)
    ]
    with pytest.raises(InvalidInput, match="maximum"):
        normalize_openai_tools(too_many)

    nested: dict = {"type": "string"}
    for _ in range(MAX_TOOL_CALL_ARGUMENTS_DEPTH + 2):
        nested = {"type": "object", "properties": {"x": nested}}
    with pytest.raises(InvalidInput, match="nesting depth"):
        normalize_openai_tools([{"type": "function", "function": {"name": "deep", "parameters": nested}}])

    huge_schema = {
        "type": "object",
        "properties": {"big": {"type": "string", "description": "a" * (MAX_TOOL_SCHEMA_CHARS + 10)}},
    }
    with pytest.raises(InvalidInput, match="exceeds"):
        normalize_openai_tools([{"type": "function", "function": {"name": "huge", "parameters": huge_schema}}])


@pytest.mark.parametrize("bad_choice", [
    "any",
    "AUTO",
    "",
    123,
    True,
    [],
    {"type": "other", "function": {"name": "get_weather"}},
    {"type": "function", "function": {"name": "get_weather"}, "extra": 1},
    {"type": "function", "function": {"name": "get_weather", "extra": 1}},
    {"type": "function", "function": {}},
    {"type": "function", "function": {"name": "not_declared"}},
    {"type": "function", "function": {"name": "bad name!"}},
])
def test_invalid_tool_choice_rejected(bad_choice):
    with pytest.raises(InvalidInput):
        normalize_openai_tool_choice(bad_choice, declared_tools=frozenset({"get_weather"}))


def test_tool_choice_and_parallel_tool_calls_require_tools():
    called = []
    gw = OpenAICompatibleGateway(_local_cfg(), transport=lambda *a: called.append(a) or {})
    with pytest.raises(InvalidInput, match="requires"):
        gw.generate(ModelRequest(prompt="hi", tier="code", options={"tool_choice": "auto"}), _ctx())
    with pytest.raises(InvalidInput, match="requires"):
        gw.generate(ModelRequest(prompt="hi", tier="code", options={"parallel_tool_calls": True}), _ctx())
    with pytest.raises(InvalidInput, match="boolean"):
        gw.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL], "parallel_tool_calls": "yes"}),
            _ctx(),
        )
    assert called == []


# -- 2. Multi-turn tool history & continuation --------------------------------


def test_multi_turn_tool_history_preserves_tool_calls_and_tool_results():
    payloads: list[dict] = []

    def transport(url, payload, headers, timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            return _tool_response([{
                "id": "call_tokyo_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo","units":"c"}'},
            }])
        return {
            "choices": [{
                "message": {"role": "assistant", "content": "It is 22C and sunny in Tokyo."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 140, "completion_tokens": 12},
        }

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    turn1 = gw.generate(
        ModelRequest(
            prompt="What is the weather in Tokyo?",
            tier="code",
            system="Use tools when helpful.",
            options={"tools": [WEATHER_TOOL], "tool_choice": "auto"},
        ),
        _ctx(),
    )
    assert isinstance(turn1, OpenAICompatibleResponse)
    assert len(turn1.tool_calls) == 1
    call = turn1.tool_calls[0]
    assert isinstance(call, OpenAIToolCall)
    assert call.id == "call_tokyo_1"
    assert call.arguments == {"city": "Tokyo", "units": "c"}

    # Turn 2: pass OpenAIToolCall object directly + tool result, empty prompt (no fake user turn).
    turn2 = gw.generate(
        ModelRequest(
            prompt="",
            tier="code",
            system="Use tools when helpful.",
            history=(
                {"role": "user", "content": "What is the weather in Tokyo?"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": '{"temp_c": 22, "condition": "sunny"}',
                },
            ),
            options={"tools": [WEATHER_TOOL]},
        ),
        _ctx(),
    )
    assert turn2.text == "It is 22C and sunny in Tokyo."
    assert turn2.tool_calls == ()
    msgs = payloads[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["content"] == ""
    assert msgs[2]["tool_calls"] == [{
        "id": "call_tokyo_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo","units":"c"}'},
    }]
    assert msgs[3] == {
        "role": "tool",
        "tool_call_id": "call_tokyo_1",
        "name": "get_weather",
        "content": '{"temp_c": 22, "condition": "sunny"}',
    }


def test_history_normalizes_dict_arguments_and_validates_tool_messages():
    seen: list[dict] = []

    def transport(url, payload, headers, timeout):
        seen.append(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    gw.generate(
        ModelRequest(
            prompt="Summarize.",
            tier="code",
            history=(
                {"role": "user", "content": "Compute 2+2"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "math_eval", "arguments": {"expression": "2+2"}}}],
                },
                {"role": "tool", "tool_call_id": "call_0", "content": "4"},
            ),
        ),
        _ctx(),
    )
    msgs = seen[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user"]
    assert msgs[1]["tool_calls"][0] == {
        "id": "call_0",
        "type": "function",
        "function": {"name": "math_eval", "arguments": '{"expression":"2+2"}'},
    }

    with pytest.raises(InvalidInput):
        gw.generate(
            ModelRequest(prompt="", tier="code", history=({"role": "tool", "content": 123},)),
            _ctx(),
        )
    with pytest.raises(InvalidInput):
        gw.generate(
            ModelRequest(prompt="", tier="code", history=({"role": "tool", "tool_call_id": "bad\nid", "content": "ok"},)),
            _ctx(),
        )
    with pytest.raises(InvalidInput):
        gw.generate(
            ModelRequest(prompt="", tier="code", history=({"role": "user", "content": "no tool tail"},)),
            _ctx(),
        )
    # Hand-constructed invalid OpenAIToolCall must still be re-validated in history.
    bogus_call = OpenAIToolCall(
        id="bad\nid", name="bad name", arguments=MappingProxyType({}), arguments_json="not-json",
    )
    with pytest.raises(InvalidInput):
        gw.generate(
            ModelRequest(
                prompt="hi",
                tier="code",
                history=({"role": "assistant", "content": "", "tool_calls": [bogus_call]},),
            ),
            _ctx(),
        )


def test_nested_mapping_proxy_schema_and_stream_control_char_name_validation():
    frozen_tool = MappingProxyType({
        "type": "function",
        "function": MappingProxyType({
            "name": "frozen_tool",
            "parameters": MappingProxyType({
                "type": "object",
                "properties": MappingProxyType({"x": MappingProxyType({"type": "string"})}),
            }),
        }),
    })
    normalized, names = normalize_openai_tools([frozen_tool])
    assert names == frozenset({"frozen_tool"})
    assert normalized[0]["function"]["parameters"]["properties"]["x"] == {"type": "string"}

    asm = ToolCallDeltaAssembler()
    with pytest.raises(DependencyUnavailable, match="control characters"):
        asm.add_delta({"index": 0, "id": "call_0", "function": {"name": "bad\r\nname"}})

    # Empty non-dict message (falsy []) must fail as invalid message rather than empty dict.
    gw = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: {"choices": [{"index": 0, "message": []}]},
    )
    with pytest.raises(DependencyUnavailable, match="invalid message"):
        gw.generate(ModelRequest(prompt="hi", tier="code"), _ctx())


# -- 3. Non-streaming tool_calls parsing & strict fail-closed checks ----------


def test_non_streaming_parallel_tool_calls_with_reasoning_and_timings():
    def transport(url, payload, headers, timeout):
        return _tool_response(
            [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'},
                },
                {
                    "id": "",  # local servers occasionally omit id -> deterministic call_1
                    "type": "function",
                    "function": {"name": "math_eval", "arguments": {"expression": "19 * 23"}},
                },
            ],
            content="Checking both now.",
            reasoning_content="Need Tokyo weather and product 19*23.",
            timings={
                "prompt_n": 90,
                "prompt_ms": 30.0,
                "predicted_n": 18,
                "predicted_ms": 90.0,
                "cache_n": 40,
            },
        )

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    resp = gw.generate(
        ModelRequest(
            prompt="Tokyo weather and 19*23",
            tier="code",
            options={"tools": [WEATHER_TOOL, MATH_TOOL], "tool_choice": "auto"},
        ),
        _ctx(),
    )
    assert isinstance(resp, OpenAICompatibleResponse)
    assert resp.text == "Checking both now."
    assert resp.reasoning_content == "Need Tokyo weather and product 19*23."
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].id == "call_a"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Tokyo"}
    assert resp.tool_calls[0].arguments_json == '{"city":"Tokyo"}'
    assert isinstance(resp.tool_calls[0].arguments, MappingProxyType)
    assert resp.tool_calls[1].id == "call_1"
    assert resp.tool_calls[1].name == "math_eval"
    assert resp.tool_calls[1].arguments == {"expression": "19 * 23"}
    assert resp.tool_calls[1].to_openai_dict() == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "math_eval", "arguments": '{"expression":"19 * 23"}'},
    }
    assert resp.telemetry is not None
    assert resp.telemetry.prompt_eval_ms == 30.0
    assert resp.telemetry.eval_ms == 90.0
    assert resp.telemetry.prompt_cached_tokens == 40
    assert resp.tool_telemetry == OpenAIToolTelemetry(
        tools_requested=2,
        tool_choice="auto",
        tool_calls_returned=2,
        tool_names=("get_weather", "math_eval"),
        streamed=False,
        stream_delta_count=0,
    )


def test_tool_choice_auto_and_none_accept_plain_text_replies():
    gw = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: {
            "choices": [{"message": {"role": "assistant", "content": "No tool needed."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
    )
    for choice in ("auto", "none", None):
        opts = {"tools": [WEATHER_TOOL]}
        if choice is not None:
            opts["tool_choice"] = choice
        resp = gw.generate(ModelRequest(prompt="Say hello", tier="fast", options=opts), _ctx())
        assert resp.text == "No tool needed."
        assert resp.tool_calls == ()
        assert resp.tool_telemetry is not None
        assert resp.tool_telemetry.tool_calls_returned == 0


def test_unsolicited_tool_calls_fail_closed_when_no_tools_requested():
    for content in (None, "Sure, calling tool"):
        gw = OpenAICompatibleGateway(
            _local_cfg(),
            transport=lambda *a, c=content: _tool_response([{
                "id": "call_unsolicited",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
            }], content=c),
        )
        with pytest.raises(DependencyUnavailable, match="no tools were requested"):
            gw.generate(ModelRequest(prompt="hi", tier="fast"), _ctx())


def test_tool_choice_policy_violations_fail_closed():
    # tool_choice="none" but server returns a tool call
    gw_none = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: _tool_response([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
        }]),
    )
    with pytest.raises(DependencyUnavailable, match="tool_choice='none'"):
        gw_none.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL], "tool_choice": "none"}),
            _ctx(),
        )

    # tool_choice="required" but server returns text only
    gw_req = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: {
            "choices": [{"message": {"role": "assistant", "content": "I refuse."}, "finish_reason": "stop"}],
        },
    )
    with pytest.raises(DependencyUnavailable, match="required tool_choice"):
        gw_req.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL], "tool_choice": "required"}),
            _ctx(),
        )

    # forced specific tool but server calls another declared tool
    gw_wrong = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: _tool_response([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "math_eval", "arguments": '{"expression":"1+1"}'},
        }]),
    )
    with pytest.raises(DependencyUnavailable, match="instead of forced"):
        gw_wrong.generate(
            ModelRequest(
                prompt="hi",
                tier="code",
                options={
                    "tools": [WEATHER_TOOL, MATH_TOOL],
                    "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                },
            ),
            _ctx(),
        )

    # undeclared tool hallucinated by server
    gw_undeclared = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: _tool_response([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "shell_exec", "arguments": '{"cmd":"whoami"}'},
        }]),
    )
    with pytest.raises(DependencyUnavailable, match="undeclared tool call"):
        gw_undeclared.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL]}),
            _ctx(),
        )


def test_required_tool_choice_empty_length_still_raises_model_call_error():
    gw = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: {
            "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "length"}],
        },
    )
    with pytest.raises(ModelCallError) as exc_info:
        gw.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL], "tool_choice": "required"}),
            _ctx(),
        )
    assert exc_info.value.kind == "empty_response"


@pytest.mark.parametrize("bad_calls", [
    "not-a-list",
    ["not-a-dict"],
    [{"id": "call_1", "type": "custom", "function": {"name": "get_weather", "arguments": "{}"}}],
    [{"id": 999, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
    [{"id": "bad\r\nid", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
    [{"id": "c" * (MAX_TOOL_CALL_ID_CHARS + 1), "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
    [
        {"id": "dup", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"A"}'}},
        {"id": "dup", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"B"}'}},
    ],
    [{"id": "call_1", "type": "function", "function": "not-a-dict"}],
    [{"id": "call_1", "type": "function", "function": {"name": "bad name", "arguments": "{}"}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather"}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": None}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":'}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '[1, 2, 3]'}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"x": NaN}'}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"x": Infinity}'}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"a":' * (MAX_TOOL_CALL_ARGUMENTS_DEPTH + 2) + '{}' + '}' * (MAX_TOOL_CALL_ARGUMENTS_DEPTH + 2)}}],
    [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"k":"' + 'x' * MAX_TOOL_CALL_ARGUMENTS_CHARS + '"}'}}],
])
def test_malformed_tool_calls_fail_closed(bad_calls):
    with pytest.raises(DependencyUnavailable):
        parse_openai_tool_calls(bad_calls, allowed_names=frozenset({"get_weather"}))


def test_too_many_tool_calls_and_non_string_content_fail_closed():
    overflow = [
        {"id": f"call_{i}", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
        for i in range(MAX_RESPONSE_TOOL_CALLS + 1)
    ]
    with pytest.raises(DependencyUnavailable, match="too many"):
        parse_openai_tool_calls(overflow, allowed_names=frozenset({"get_weather"}))

    gw = OpenAICompatibleGateway(
        _local_cfg(),
        transport=lambda *a: _tool_response(
            [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'}}],
            content=["not", "a", "string"],
        ),
    )
    with pytest.raises(DependencyUnavailable, match="invalid message content"):
        gw.generate(ModelRequest(prompt="hi", tier="code", options={"tools": [WEATHER_TOOL]}), _ctx())


# -- 4. Streaming tool_call delta assembly (unit + real loopback SSE) ---------


def test_delta_assembler_handles_interleaved_split_name_and_repeated_headers():
    asm = ToolCallDeltaAssembler()
    # Out-of-order index 1 arrives first with split function name across two chunks.
    d1 = asm.add_delta({
        "index": 1, "id": "call_math", "type": "function",
        "function": {"name": "math_", "arguments": ""},
    })
    assert d1["id"] == "call_math" and d1["function"] == {"name": "math_", "arguments": ""}

    # Index 0 arrives interleaved with idempotent proxy header repeat on chunk 2.
    asm.add_delta({
        "index": 0, "id": "call_wx", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":'},
    })
    d2 = asm.add_delta({
        "index": 1, "id": None, "type": None,
        "function": {"name": "eval", "arguments": '{"expression":'},
    })
    assert d2 == {"index": 1, "function": {"name": "eval", "arguments": '{"expression":'}}

    d3 = asm.add_delta({
        "index": 0, "id": "call_wx", "type": "function",
        "function": {"name": "get_weather", "arguments": '"Tokyo"}'},
    })
    # Repeated id+name is suppressed rather than doubled to "get_weatherget_weather".
    assert d3 == {"index": 0, "function": {"arguments": '"Tokyo"}'}}

    asm.add_delta({
        "index": 1,
        "function": {"arguments": '"6*7"}'},
    })
    assert asm.delta_count == 5
    assert asm.call_count == 2
    calls = asm.finalize(allowed_names=frozenset({"get_weather", "math_eval"}))
    assert [c.id for c in calls] == ["call_wx", "call_math"]
    assert [c.name for c in calls] == ["get_weather", "math_eval"]
    assert calls[0].arguments == {"city": "Tokyo"}
    assert calls[1].arguments == {"expression": "6*7"}


def test_delta_assembler_feed_chunk_and_rejections():
    asm = ToolCallDeltaAssembler(max_calls=4, max_argument_chars=32)
    assert asm.feed_chunk({"choices": [{"delta": {"content": "hi"}}]}) == []
    assert asm.feed_chunk({"choices": []}) == []

    emitted = asm.feed_chunk({
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0, "id": "call_0", "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                }],
            },
        }],
    })
    assert len(emitted) == 1
    assert asm.finalize(allowed_names=frozenset({"get_time"}))[0].arguments == {}

    # Rejections
    bad_asm = ToolCallDeltaAssembler(max_calls=2, max_argument_chars=16)
    with pytest.raises(DependencyUnavailable):
        bad_asm.add_delta("not-a-dict")
    with pytest.raises(DependencyUnavailable):
        bad_asm.add_delta({"index": -1})
    with pytest.raises(DependencyUnavailable):
        bad_asm.add_delta({"index": True})
    with pytest.raises(DependencyUnavailable):
        bad_asm.add_delta({"index": 2})  # == max_calls
    with pytest.raises(DependencyUnavailable):
        bad_asm.add_delta({"index": 0, "type": "other"})
    with pytest.raises(DependencyUnavailable):
        bad_asm.feed_chunk({"choices": [{"delta": {"tool_calls": "bad"}}]})

    # Conflicting id on same index & duplicate id across indexes
    id_asm = ToolCallDeltaAssembler()
    id_asm.add_delta({"index": 0, "id": "call_a", "function": {"name": "get_time", "arguments": "{"}})
    with pytest.raises(DependencyUnavailable, match="conflicting"):
        id_asm.add_delta({"index": 0, "id": "call_b"})
    with pytest.raises(DependencyUnavailable, match="duplicate"):
        id_asm.add_delta({"index": 1, "id": "call_a"})

    # Oversized arguments fail immediately mid-stream
    cap_asm = ToolCallDeltaAssembler(max_argument_chars=12)
    cap_asm.add_delta({"index": 0, "id": "call_x", "function": {"name": "get_time", "arguments": '{"a":"123"'}})
    with pytest.raises(DependencyUnavailable, match="oversized"):
        cap_asm.add_delta({"index": 0, "function": {"arguments": ',"b":"456789"}'}})

    # Truncated JSON fails on finalize
    trunc_asm = ToolCallDeltaAssembler()
    trunc_asm.add_delta({"index": 0, "id": "call_t", "function": {"name": "get_time", "arguments": '{"partial":'}})
    with pytest.raises(DependencyUnavailable, match="not valid JSON"):
        trunc_asm.finalize()


def test_end_to_end_loopback_sse_streams_split_name_reasoning_and_parallel_tool_calls():
    frames = [
        json.dumps({
            "id": "chatcmpl-sse-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Let me check."}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-sse-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_0", "type": "function",
                "function": {"name": "get_", "arguments": ""},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-sse-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"name": "weather", "arguments": '{"city":'},
            }, {
                "index": 1, "id": "call_1", "type": "function",
                "function": {"name": "math_eval", "arguments": '{"expression":"2+2"}'},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-sse-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '"Osaka"}'},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-sse-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 115, "completion_tokens": 27, "total_tokens": 142},
            "timings": {"prompt_n": 115, "prompt_ms": 20.0, "predicted_n": 27, "predicted_ms": 135.0},
        }),
        "[DONE]",
    ]
    captured: list[dict] = []
    reasoning_seen: list[str] = []
    tool_deltas_seen: list[dict] = []
    content_seen: list[str] = []

    live = stream_sink.LiveTurnStream(
        lambda text: (content_seen.append(text), True)[1],
        write_reasoning=lambda text: (reasoning_seen.append(text), True)[1],
        write_tool_call=lambda delta: (tool_deltas_seen.append(delta), True)[1],
    )

    with _serve_sse(frames, capture=captured) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}", model="bonsai-9b-mtp"))
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            resp = gw.generate(
                ModelRequest(
                    prompt="Osaka weather and 2+2",
                    tier="fast",
                    options={"tools": [WEATHER_TOOL, MATH_TOOL], "tool_choice": "auto"},
                ),
                _ctx(),
            )

    assert captured[0]["payload"]["stream"] is True
    assert captured[0]["payload"]["stream_options"] == {"include_usage": True}
    assert captured[0]["accept"] == "text/event-stream"
    assert reasoning_seen == ["Let me check."]
    assert len(tool_deltas_seen) == 4
    assert content_seen == []
    assert live.tool_call_deltas_forwarded == 4

    assert isinstance(resp, OpenAICompatibleResponse)
    assert resp.text == ""
    assert resp.reasoning_content == "Let me check."
    assert resp.finish_reason == "tool_calls"
    assert resp.tokens_in == 115 and resp.tokens_out == 27
    assert resp.telemetry is not None and resp.telemetry.eval_ms == 135.0
    assert len(resp.tool_calls) == 2
    # Proves split function name ("get_" + "weather") survived SSE assembly cleanly!
    assert resp.tool_calls[0].id == "call_0"
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Osaka"}
    assert resp.tool_calls[1].id == "call_1"
    assert resp.tool_calls[1].name == "math_eval"
    assert resp.tool_calls[1].arguments == {"expression": "2+2"}
    assert resp.tool_telemetry == OpenAIToolTelemetry(
        tools_requested=2,
        tool_choice="auto",
        tool_calls_returned=2,
        tool_names=("get_weather", "math_eval"),
        streamed=True,
        stream_delta_count=4,
    )


def test_end_to_end_sse_with_request_stream_true_without_live_sink():
    frames = [
        json.dumps({
            "id": "chatcmpl-sse-2",
            "object": "chat.completion.chunk",
            "model": "bonsai-27b-pq2",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_z", "type": "function",
                "function": {"name": "get_time", "arguments": "{}"},
            }]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 9},
        }),
        "[DONE]",
    ]
    with _serve_sse(frames) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}"))
        resp = gw.generate(
            ModelRequest(
                prompt="What time is it?",
                tier="code",
                stream=True,
                options={"tools": [ZERO_ARG_TOOL], "tool_choice": "required"},
            ),
            _ctx(),
        )
    assert resp.tool_calls[0].name == "get_time"
    assert resp.tool_calls[0].arguments == {}
    assert resp.tool_telemetry is not None
    assert resp.tool_telemetry.streamed is True
    assert resp.tool_telemetry.stream_delta_count == 1


def test_sse_rejects_unsolicited_tool_call_delta_before_forwarding_to_live_client():
    frames = [
        json.dumps({
            "id": "chatcmpl-bad",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_sneaky", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"X"}'},
            }]}, "finish_reason": "tool_calls"}],
        }),
        "[DONE]",
    ]
    forwarded: list[dict] = []
    live = stream_sink.LiveTurnStream(
        lambda _t: True,
        write_tool_call=lambda delta: (forwarded.append(delta), True)[1],
    )
    with _serve_sse(frames) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}"))
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            with pytest.raises(DependencyUnavailable, match="not allowed"):
                gw.generate(ModelRequest(prompt="plain chat", tier="fast"), _ctx())
    assert forwarded == []


def test_sse_client_disconnect_during_tool_delta_cancels_cleanly():
    frames = [
        json.dumps({
            "id": "chatcmpl-disc",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b-mtp",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_0", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
            }]}, "finish_reason": "tool_calls"}],
        }),
        "[DONE]",
    ]
    live = stream_sink.LiveTurnStream(lambda _t: True, write_tool_call=lambda _d: False)
    with _serve_sse(frames) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}"))
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            with pytest.raises(Cancelled):
                gw.generate(
                    ModelRequest(prompt="hi", tier="fast", options={"tools": [WEATHER_TOOL]}),
                    _ctx(),
                )
    assert gw.last_response_meta == {}


def test_sse_truncated_arguments_and_conflicting_ids_fail_closed():
    trunc_frames = [
        json.dumps({
            "id": "chatcmpl-trunc",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_0", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":'},
            }]}, "finish_reason": "tool_calls"}],
        }),
        "[DONE]",
    ]
    with _serve_sse(trunc_frames) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}"))
        with pytest.raises(DependencyUnavailable, match="not valid JSON"):
            gw.generate(
                ModelRequest(prompt="hi", tier="fast", stream=True, options={"tools": [WEATHER_TOOL]}),
                _ctx(),
            )

    conflict_frames = [
        json.dumps({
            "id": "chatcmpl-conf",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_a", "type": "function",
                "function": {"name": "get_weather", "arguments": "{"},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-conf",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_b", "function": {"arguments": "}"},
            }]}, "finish_reason": "tool_calls"}],
        }),
        "[DONE]",
    ]
    with _serve_sse(conflict_frames) as port:
        gw = OpenAICompatibleGateway(_local_cfg(base_url=f"http://127.0.0.1:{port}"))
        with pytest.raises(DependencyUnavailable, match="conflicting"):
            gw.generate(
                ModelRequest(prompt="hi", tier="fast", stream=True, options={"tools": [WEATHER_TOOL]}),
                _ctx(),
            )


# -- 5. Telemetry, privacy boundaries, and spend accounting -------------------


def test_telemetry_and_logs_are_content_free_and_report_validation_failures(caplog, monkeypatch):
    secret_prompt = "SECRET-USER-PROMPT-999"
    secret_arg = "SECRET-TOOL-ARGUMENT-888"
    secret_reasoning = "SECRET-CHAIN-OF-THOUGHT-777"
    secret_key = "sk-secret-tool-key-666"
    observed_metrics: list[tuple[str, object]] = []
    monkeypatch.setattr(
        default_registry(), "observe_inference",
        lambda backend, telemetry: observed_metrics.append((backend, telemetry)),
    )

    gw = OpenAICompatibleGateway(
        _local_cfg(api_key=secret_key),
        transport=lambda *a: _tool_response(
            [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": secret_arg})},
            }],
            content=None,
            reasoning_content=secret_reasoning,
            timings={"prompt_n": 40, "prompt_ms": 10.0, "predicted_n": 10, "predicted_ms": 50.0},
        ),
    )

    with caplog.at_level("DEBUG"), activity_tracker.response_span("tools-telemetry") as span:
        resp = gw.generate(
            ModelRequest(
                prompt=secret_prompt,
                tier="code",
                options={"tools": [WEATHER_TOOL], "tool_choice": "required"},
            ),
            _ctx(),
        )
        public = activity_tracker.public_response(span, include_detail=True)

    outcome = next(e for e in public["events"] if e["kind"] == "inference_outcome")
    summary = json.loads(outcome["summary"])
    assert summary["finish_reason"] == "tool_calls"
    assert summary["tools_requested"] == 1
    assert summary["tool_choice"] == "required"
    assert summary["tool_calls_returned"] == 1
    assert summary["tool_names"] == ["get_weather"]
    assert gw.last_response_meta["tool_calls_returned"] == 1
    assert observed_metrics and observed_metrics[-1][0] == "openai_compatible"
    assert observed_metrics[-1][1] is resp.telemetry

    # Strict privacy boundary: no prompt, tool arguments, reasoning, or API key in logs or telemetry.
    blob = repr(public) + repr(gw.last_response_meta) + caplog.text
    for secret in (secret_prompt, secret_arg, secret_reasoning, secret_key):
        assert secret not in blob

    # Failed tool validation clears last_response_meta and emits bounded failure event.
    bad_gw = OpenAICompatibleGateway(
        _local_cfg(api_key=secret_key),
        transport=lambda *a: _tool_response([{
            "id": "call_bad",
            "type": "function",
            "function": {"name": "undeclared_fn", "arguments": json.dumps({"secret": secret_arg})},
        }]),
    )
    with activity_tracker.response_span("tools-fail-telemetry") as fail_span:
        with pytest.raises(DependencyUnavailable):
            bad_gw.generate(
                ModelRequest(prompt=secret_prompt, tier="code", options={"tools": [WEATHER_TOOL]}),
                _ctx(),
            )
        fail_public = activity_tracker.public_response(fail_span, include_detail=True)

    assert bad_gw.last_response_meta == {}
    fail_event = next(e for e in fail_public["events"] if e["kind"] == "openai_tool_validation_failed")
    assert json.loads(fail_event["summary"]) == {
        "reason": "undeclared_tool",
        "streamed": False,
        "tools_requested": 1,
    }
    assert secret_arg not in repr(fail_public)
    assert secret_key not in repr(fail_public)


def test_metered_remote_spend_reserves_tool_schema_bytes_and_accounts_tool_calls(tmp_path):
    ledger = SpendLedger(
        tmp_path / "openai-spend.json",
        clock=lambda: 1_700_000_000.0,
        filename="openai-spend.json",
        schema="sonder.openai_spend.v1",
        label="OpenAI-compatible",
    )
    large_tool = {
        "type": "function",
        "function": {
            "name": "big_tool",
            "description": "d" * 4000,
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }
    cfg_tight = OpenAICompatibleConfig(
        base_url="https://api.example.com",
        api_key="sk-test-spend",
        model="m",
        price=ChatPrice(Decimal(100), Decimal(100)),
        daily_usd=Decimal("0.10"),
        max_tokens_ceiling=10,
    )
    called = []
    gw_tight = OpenAICompatibleGateway(
        cfg_tight, transport=lambda *a: called.append(a) or {}, spend_ledger=ledger,
    )
    # 4000+ tool bytes * $100/M = > $0.40 > $0.10 cap -> refused BEFORE send.
    with pytest.raises(OpenAIDailyCapReached):
        gw_tight.generate(
            ModelRequest(prompt="hi", tier="code", options={"tools": [large_tool]}),
            _ctx(cloud=True),
        )
    assert called == []

    # With sufficient cap, actual usage is charged on a tool-call-only response.
    cfg_ok = OpenAICompatibleConfig(
        base_url="https://api.example.com",
        api_key="sk-test-spend",
        model="m",
        price=ChatPrice(Decimal(1), Decimal(2)),
        daily_usd=Decimal(10),
        max_tokens_ceiling=100,
    )
    gw_ok = OpenAICompatibleGateway(
        cfg_ok,
        transport=lambda *a: _tool_response(
            [{"id": "call_1", "type": "function", "function": {"name": "big_tool", "arguments": '{"x":"1"}'}}],
            usage={"prompt_tokens": 1000, "completion_tokens": 500},
        ),
        spend_ledger=ledger,
    )
    resp = gw_ok.generate(
        ModelRequest(prompt="hi", tier="code", options={"tools": [large_tool]}),
        _ctx(cloud=True),
    )
    assert len(resp.tool_calls) == 1
    assert ledger.today_usd() == Decimal("0.002000")


def test_generate_accepts_minimal_duck_typed_request_without_stream_or_history():
    captured: list[dict] = []

    def transport(url, payload, headers, timeout):
        captured.append(payload)
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    gw = OpenAICompatibleGateway(_local_cfg(), transport=transport)
    stub = type("MinimalRequest", (), {"options": {}, "tier": "code", "prompt": "hello", "system": None})()
    resp = gw.generate(stub, _ctx())
    assert resp.text == "ok"
    assert captured[0]["stream"] is False
    assert captured[0]["messages"] == [{"role": "user", "content": "hello"}]

    empty_stub = type("EmptyRequest", (), {"options": {}, "tier": "code", "prompt": "   ", "system": None})()
    with pytest.raises(InvalidInput, match="prompt is empty"):
        gw.generate(empty_stub, _ctx())
