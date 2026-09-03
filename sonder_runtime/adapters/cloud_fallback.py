"""One hosted request with the documented K3-to-K2.7 availability fallback.

An unfunded Kimi K3 request answers HTTP 402 deterministically; ordinary
single-model work may then spend the plan-covered allowance through K2.7,
while immutable fanout rows never substitute a target. It reads the
transport's ``ModelCallError``, so it lives with the adapters. Moved from
``server.py`` in the WP1 Three-Hundred-Thirtieth Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

from sonder_runtime.adapters.model_transport import ModelCallError


def extra_usage_fallback(model, error, *, fallback_model):
    """Return the plan-covered Kimi fallback for an unfunded K3 request.

    Ollama currently bills Kimi K3 only against the separate extra-usage
    balance, even for Pro/Max accounts.  A 402 is therefore a deterministic
    model-availability decision, not a transient transport failure.  Honor an
    explicit K3 selection, but let opted-in cloud work consume the account's
    ordinary resettable allowance through K2.7 when that balance is empty.

    ``fallback_model`` is the configured plan-covered model; it is injected
    because the runtime model configuration stays with the composition root.
    """
    if not isinstance(error, ModelCallError) or error.status != 402:
        return None
    if not str(model or "").strip().casefold().startswith("kimi-k3:"):
        return None
    if str(model).strip().casefold() == fallback_model.casefold():
        return None
    return fallback_model


def chat_request_with_cloud_fallback(
    payload, *, model, timeout=None, cancel_check=None,
    accept_native_tool_calls=False, compact_cloud_reasoning=False,
    allow_cloud_fallback=True, chat_request, apply_thinking_policy, fallback_model,
):
    """Make one cloud request, optionally falling back once on K3 HTTP 402.

    A normal single-model request may use the documented K3-to-K2.7
    availability fallback.  Durable fanout rows are different: each row is an
    immutable, caller-visible target and must never attribute another model's
    response (or spend) to it.  Those callers pass ``allow_cloud_fallback``
    false so the requested target's provider error is recorded directly.

    ``chat_request`` performs one transport call, ``apply_thinking_policy``
    applies the hosted thinking controls to the fallback payload and
    ``fallback_model`` is the configured plan-covered model; all three are
    injected so the root delegate keeps the transport's monkeypatch seams.
    """
    try:
        out, content = chat_request(
            payload,
            model=model,
            cloud=True,
            timeout=timeout,
            cancel_check=cancel_check,
            accept_native_tool_calls=accept_native_tool_calls,
            idempotent=True,
        )
        return out, content, model
    except ModelCallError as error:
        if not allow_cloud_fallback:
            raise
        fallback = extra_usage_fallback(model, error, fallback_model=fallback_model)
        if fallback is None:
            raise

    fallback_payload = dict(payload)
    fallback_payload["model"] = fallback
    fallback_payload.pop("think", None)
    apply_thinking_policy(
        fallback_payload, fallback, compact=compact_cloud_reasoning,
    )
    out, content = chat_request(
        fallback_payload,
        model=fallback,
        cloud=True,
        timeout=timeout,
        cancel_check=cancel_check,
        accept_native_tool_calls=accept_native_tool_calls,
        idempotent=True,
    )
    return out, content, fallback
