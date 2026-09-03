"""The hosted availability fallback lives in the adapters layer; root names are delegates."""
import pytest

import server
from sonder_runtime.adapters import cloud_fallback
from sonder_runtime.adapters.model_transport import ModelCallError

FALLBACK = "kimi-k2.7-code:cloud"


def test_extra_usage_fallback_applies_only_to_unfunded_k3_requests():
    unfunded = ModelCallError("http", "payment required", status=402)
    assert cloud_fallback.extra_usage_fallback("kimi-k3:cloud", unfunded, fallback_model=FALLBACK) == FALLBACK
    assert cloud_fallback.extra_usage_fallback("KIMI-K3:cloud", unfunded, fallback_model=FALLBACK) == FALLBACK
    assert cloud_fallback.extra_usage_fallback(FALLBACK, unfunded, fallback_model=FALLBACK) is None
    assert cloud_fallback.extra_usage_fallback("glm-5.2:cloud", unfunded, fallback_model=FALLBACK) is None
    assert cloud_fallback.extra_usage_fallback("kimi-k3:cloud", ModelCallError("http", "x", status=500), fallback_model=FALLBACK) is None
    assert cloud_fallback.extra_usage_fallback("kimi-k3:cloud", RuntimeError("402"), fallback_model=FALLBACK) is None
    assert server._cloud_extra_usage_fallback("kimi-k3:cloud", unfunded) == server.CLOUD_EXTRA_USAGE_FALLBACK_MODEL


def _transport(status_for_k3=402):
    calls = []

    def chat_request(payload, **kwargs):
        calls.append((dict(payload), kwargs))
        if payload["model"].startswith("kimi-k3:"):
            raise ModelCallError("http", "unfunded", status=status_for_k3)
        return {"model": payload["model"]}, "answer"

    return chat_request, calls


def test_a_402_on_k3_falls_back_once_with_thinking_policy_reapplied():
    chat_request, calls = _transport()
    applied = []

    def thinking(payload, model, *, compact=False):
        applied.append((model, compact))
        payload["think"] = "applied"

    out, content, used = cloud_fallback.chat_request_with_cloud_fallback(
        {"model": "kimi-k3:cloud", "think": True}, model="kimi-k3:cloud", timeout=9,
        compact_cloud_reasoning=True, chat_request=chat_request, apply_thinking_policy=thinking,
        fallback_model=FALLBACK,
    )
    assert (out, content, used) == ({"model": FALLBACK}, "answer", FALLBACK)
    assert applied == [(FALLBACK, True)]
    assert [payload["model"] for payload, _kw in calls] == ["kimi-k3:cloud", FALLBACK]
    assert calls[1][0]["think"] == "applied"
    assert calls[1][1] == {
        "model": FALLBACK, "cloud": True, "timeout": 9, "cancel_check": None,
        "accept_native_tool_calls": False, "idempotent": True,
    }


def test_immutable_targets_and_other_failures_never_substitute():
    chat_request, calls = _transport()
    with pytest.raises(ModelCallError):
        cloud_fallback.chat_request_with_cloud_fallback(
            {"model": "kimi-k3:cloud"}, model="kimi-k3:cloud", allow_cloud_fallback=False,
            chat_request=chat_request, apply_thinking_policy=lambda *a, **k: None, fallback_model=FALLBACK,
        )
    assert len(calls) == 1
    chat_request, calls = _transport(status_for_k3=503)
    with pytest.raises(ModelCallError):
        cloud_fallback.chat_request_with_cloud_fallback(
            {"model": "kimi-k3:cloud"}, model="kimi-k3:cloud",
            chat_request=chat_request, apply_thinking_policy=lambda *a, **k: None, fallback_model=FALLBACK,
        )
    assert len(calls) == 1


def test_root_delegate_uses_the_server_transport_seam(monkeypatch):
    chat_request, calls = _transport()
    monkeypatch.setattr(server, "_chat_request", chat_request)
    out, content, used = server._chat_request_with_cloud_fallback(
        {"model": "kimi-k3:cloud"}, model="kimi-k3:cloud",
    )
    assert used == server.CLOUD_EXTRA_USAGE_FALLBACK_MODEL
    assert content == "answer"
    assert [payload["model"] for payload, _kw in calls] == ["kimi-k3:cloud", used]
