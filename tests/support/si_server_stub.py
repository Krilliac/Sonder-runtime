"""Reusable test helper binding a real SonderInferenceGateway with fake transport into ``server``."""
from __future__ import annotations

import json
import types
from collections.abc import Callable
from typing import Any

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env


def _to_openai_response(reply: Any, *, model: str = "sonder:latest") -> dict[str, Any]:
    """Convert either an OpenAI-shaped dict, an Ollama-shaped dict, or a plain string."""
    if isinstance(reply, str):
        return {
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
        }
    if not isinstance(reply, dict):
        return {}
    if "choices" in reply:
        return reply
    message = reply.get("message")
    if isinstance(message, dict):
        msg: dict[str, Any] = {
            "role": str(message.get("role") or "assistant"),
            "content": message.get("content", ""),
        }
        if "thinking" in message and message["thinking"] is not None:
            msg["reasoning_content"] = message["thinking"]
        if "tool_calls" in message and message["tool_calls"]:
            tool_calls = []
            for idx, tc in enumerate(message["tool_calls"]):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                args = fn.get("arguments", "{}")
                if not isinstance(args, str):
                    args = json.dumps(args)
                tool_calls.append({
                    "id": str(tc.get("id") or f"call_{idx}"),
                    "type": "function",
                    "function": {"name": str(fn.get("name") or ""), "arguments": args},
                })
            if tool_calls:
                msg["tool_calls"] = tool_calls
        out: dict[str, Any] = {
            "model": str(reply.get("model") or model),
            "choices": [{
                "index": 0,
                "message": msg,
                "finish_reason": str(reply.get("done_reason") or "stop"),
            }],
        }
        usage: dict[str, Any] = {}
        if isinstance(reply.get("prompt_eval_count"), int):
            usage["prompt_tokens"] = reply["prompt_eval_count"]
        if isinstance(reply.get("eval_count"), int):
            usage["completion_tokens"] = reply["eval_count"]
        if isinstance(reply.get("prompt_eval_cached_count"), int):
            usage["prompt_tokens_details"] = {
                "cached_tokens": reply["prompt_eval_cached_count"],
            }
        if usage:
            out["usage"] = usage
        return out
    if "response" in reply:
        return {
            "model": str(reply.get("model") or model),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": str(reply["response"])},
                "finish_reason": str(reply.get("done_reason") or "stop"),
            }],
        }
    return reply


def make_si_gateway(
    handler: Callable[[str, dict[str, Any], dict[str, str], float], Any],
    *,
    base_url: str = "http://127.0.0.1:11437",
    models: tuple[str, ...] = ("sonder:latest", "fixture"),
    default_model: str = "sonder:latest",
) -> SonderInferenceGateway:
    """Build a real ``SonderInferenceGateway`` backed by an in-memory transport callable."""
    health_body = json.dumps({
        "status": "ready",
        "api_version": 1,
        "version": "1.0.0-test",
        "features": {"structured_output": True, "tools": True},
        "models": [
            {
                "id": m,
                "state": "resident",
                "capabilities": ["completion", "chat", "tools", "structured_output"],
                "structured_output": True,
                "tools": True,
            }
            for m in models
        ],
    }).encode("utf-8")

    def get_transport(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        if url.endswith("/v1/sonder/health"):
            return (200, health_body)
        if url.endswith("/v1/models"):
            return (200, json.dumps({
                "object": "list",
                "data": [{"id": m, "capabilities": ["completion", "chat", "tools"]} for m in models],
            }).encode("utf-8"))
        return (404, b"{}")

    def transport(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        raw = handler(url, payload, headers, timeout)
        req_model = str(payload.get("model") or "").strip()
        resolved_model = default_model if not req_model or req_model == "default" else req_model
        return _to_openai_response(raw, model=resolved_model)

    return SonderInferenceGateway(
        SonderInferenceConfig(base_url=base_url, model=default_model),
        transport=transport,
        get_transport=get_transport,
    )


def bind_fake_si_server(
    monkeypatch: Any,
    server_mod: Any,
    handler: Callable[[str, dict[str, Any], dict[str, str], float], Any],
    *,
    session_capture: Any = None,
    models: tuple[str, ...] = ("sonder:latest", "fixture"),
    default_model: str = "sonder:latest",
) -> SonderInferenceGateway:
    """Bind a real ``SonderInferenceGateway`` (with fake transport) into ``server._application``."""
    gw = make_si_gateway(handler, models=models, default_model=default_model)
    bindings = provider_bindings_from_env()
    app_attrs: dict[str, Any] = {
        "model_gateway": gw,
        "provider_bindings": bindings,
    }
    if session_capture is not None:
        app_attrs["session_capture_service"] = lambda: session_capture
    app = types.SimpleNamespace(**app_attrs)
    monkeypatch.setattr(server_mod, "_application", lambda: app)
    monkeypatch.setattr(server_mod, "_APP_GRAPH", app)
    return gw
