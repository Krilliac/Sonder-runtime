"""Reusable test helper binding a real SonderInferenceGateway with fake transport into ``server``."""
from __future__ import annotations

import inspect
import json
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


def _from_openai_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI ``/v1/chat/completions`` payload back to Ollama-shaped keys for legacy post stubs."""
    out: dict[str, Any] = {
        "model": payload.get("model", ""),
        "messages": list(payload.get("messages") or ()),
        "stream": bool(payload.get("stream", False)),
    }
    opts: dict[str, Any] = {}
    if "max_tokens" in payload and payload["max_tokens"] is not None:
        opts["num_predict"] = payload["max_tokens"]
    if "temperature" in payload and payload["temperature"] is not None:
        opts["temperature"] = payload["temperature"]
    if "top_p" in payload and payload["top_p"] is not None:
        opts["top_p"] = payload["top_p"]
    if "top_k" in payload and payload["top_k"] is not None:
        opts["top_k"] = payload["top_k"]
    if "seed" in payload and payload["seed"] is not None:
        opts["seed"] = payload["seed"]
    if "stop" in payload and payload["stop"] is not None:
        opts["stop"] = payload["stop"]
    if "num_ctx" in payload and payload["num_ctx"] is not None:
        opts["num_ctx"] = payload["num_ctx"]
    if opts:
        out["options"] = opts
    ctk = payload.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        out["think"] = bool(ctk["enable_thinking"])
    rf = payload.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            out["format"] = "json"
        elif rf.get("type") == "json_schema" and isinstance(rf.get("json_schema"), dict):
            out["format"] = rf["json_schema"].get("schema")
    if "tools" in payload and payload["tools"]:
        out["tools"] = payload["tools"]
    if "tool_choice" in payload and payload["tool_choice"] is not None:
        out["tool_choice"] = payload["tool_choice"]
    return out


def bind_fake_si_post(
    monkeypatch: Any,
    server_mod: Any,
    post_fn: Callable[..., Any],
    *,
    session_capture: Any = None,
    models: tuple[str, ...] = ("sonder:latest", "fixture"),
    default_model: str = "sonder:latest",
) -> SonderInferenceGateway:
    """Bind a fake ``_post(path, payload, *args, **kwargs)`` onto both ``server._post`` and the SI gateway."""
    monkeypatch.setattr(server_mod, "_post", post_fn)

    sig = inspect.signature(post_fn)
    params = list(sig.parameters.values())
    accepts_timeout = any(
        p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        or p.name == "timeout"
        for p in params
    ) or len(params) >= 3

    def transport(url: str, openai_payload: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
        del url, headers
        ollama_like = _from_openai_request(openai_payload)
        req_model = str(ollama_like.get("model") or "").strip()
        if not req_model or req_model == "default":
            ollama_like["model"] = default_model
        try:
            if accepts_timeout:
                return post_fn("/v1/chat/completions", ollama_like, timeout)
            return post_fn("/v1/chat/completions", ollama_like)
        except Exception as exc:
            if getattr(exc, "kind", None) == "cancelled":
                from sonder_runtime.domain.common.errors import Cancelled

                raise Cancelled(str(getattr(exc, "detail", None) or exc)) from exc
            raise

    return bind_fake_si_server(
        monkeypatch,
        server_mod,
        transport,
        session_capture=session_capture,
        models=models,
        default_model=default_model,
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
    real_application = server_mod._application

    class _StubApp:
        def __init__(self) -> None:
            self.model_gateway = gw
            self.provider_bindings = bindings
            self._real: Any = None
            if session_capture is not None:
                self.session_capture_service = lambda: session_capture

        def _ensure_real(self) -> Any:
            if self._real is None:
                from sonder_runtime.bootstrap import app as _bootstrap_app

                self._real = _bootstrap_app.build_application(
                    preference_connection_factory=lambda: server_mod._open_db(),
                    preference_module_provider=lambda: server_mod.preference_learning,
                )
            return self._real

        def __getattr__(self, name: str) -> Any:
            return getattr(self._ensure_real(), name)

    del real_application
    app = _StubApp()
    monkeypatch.setattr(server_mod, "_application", lambda: app)
    monkeypatch.setattr(server_mod, "_APP_GRAPH", app)
    return gw
