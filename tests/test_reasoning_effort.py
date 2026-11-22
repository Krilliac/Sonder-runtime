"""End-to-end tests for reasoning effort domain, session store, gateways, and HTTP/REPL wiring."""
from __future__ import annotations

import threading
import pytest

from sonder_runtime.adapters import chat_effort
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.chat.provider_bridge import model_request_from_ollama_payload
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.effort_settings import STORE, SessionEffortStore, effort_command
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.reasoning_effort import (
    EFFORT_LEVELS,
    check_effort,
    parse_effort_setting,
    resolve_effort,
)
from sonder_runtime.interfaces.http import serve


def test_effort_validation_and_aliases():
    assert check_effort(None) is None
    assert check_effort("HIGH") == "high"
    assert check_effort("none") == "off"
    assert check_effort("min") == "minimal"
    assert check_effort("med") == "medium"
    assert check_effort("max") == "xhigh"
    assert parse_effort_setting("auto") is None
    assert parse_effort_setting("default") is None
    assert parse_effort_setting("reset") is None
    assert parse_effort_setting("low") == "low"
    for level in EFFORT_LEVELS:
        assert check_effort(level) == level
    with pytest.raises(InvalidInput):
        check_effort("turbo")
    with pytest.raises(InvalidInput):
        check_effort(True)


def test_effort_precedence():
    assert resolve_effort().source == "preset"
    assert resolve_effort(lane="off").value == "off"
    assert resolve_effort(lane="off").source == "lane"
    assert resolve_effort(session="low", lane="off").value == "low"
    assert resolve_effort(session="low", lane="off").source == "session"
    assert resolve_effort(request="high", session="low", lane="off").value == "high"
    assert resolve_effort(request="high", session="low", lane="off").source == "request"


def test_session_effort_store_and_slash_command():
    store = SessionEffortStore()
    assert store.get("sess-1") is None
    assert store.set("sess-1", "high", owner="alice") == "high"
    assert store.get("sess-1") == "high"
    assert store.set("sess-1", None, owner="alice") is None
    assert store.get("sess-1") is None

    try:
        STORE.clear()
        msg_show = effort_command("", "sess-cmd", lane="off")
        assert "off" in msg_show
        msg_set = effort_command("high", "sess-cmd", lane="off")
        assert "high" in msg_set
        assert STORE.get("sess-cmd") == "high"
        msg_auto = effort_command("auto", "sess-cmd", lane="off")
        assert "auto" in msg_auto
        assert STORE.get("sess-cmd") is None
        msg_bad = effort_command("bogus", "sess-cmd")
        assert "reasoning_effort must be" in msg_bad
    finally:
        STORE.clear()


def test_provider_bridge_forwards_reasoning_effort():
    req = model_request_from_ollama_payload(
        {
            "model": "qwen",
            "messages": [{"role": "user", "content": "hi"}],
            "options": {"reasoning_effort": "low"},
        },
        tier="code",
        provider="openai_compatible",
    )
    assert req.options["reasoning_effort"] == "low"


def test_openai_compat_gateway_maps_off_to_enable_thinking_false():
    captured = []

    def transport(url, payload, headers, timeout):
        captured.append(payload)
        return {
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    from sonder_runtime.application.context import _NeverCancelled
    cfg = OpenAICompatibleConfig(base_url="http://127.0.0.1:11438", model="test-model")
    gw = OpenAICompatibleGateway(cfg, transport=transport)
    ctx = OperationContext(
        correlation_id="test",
        principal_id="owner",
        auth_level="local",
        source="http",
        deadline_monotonic=None,
        cancellation=_NeverCancelled(),
        cloud_allowed=False,
    )

    gw.generate(ModelRequest(prompt="hi", tier="code", options={"reasoning_effort": "off"}), ctx)
    gw.generate(ModelRequest(prompt="hi", tier="code", options={"reasoning_effort": "high"}), ctx)

    assert captured[0]["chat_template_kwargs"] == {
        "reasoning_effort": "off",
        "enable_thinking": False,
    }
    assert captured[1]["chat_template_kwargs"] == {
        "reasoning_effort": "high",
    }


def test_bind_current_activity_propagates_effort_scope_to_worker_thread():
    observed = []
    with chat_effort.effort_scope("high"):
        wrapper = serve._bind_current_activity()
        assert wrapper is not None
        t = threading.Thread(target=wrapper(lambda: observed.append(chat_effort.active_turn_effort())))
        t.start()
        t.join(timeout=5)
    assert observed == ["high"]
    assert chat_effort.active_turn_effort() is None
