"""OpenAI wire translation for Sonder Inference ``/v1/chat/completions`` and ``/v1/completions``.

Translates legacy Ollama-shaped request payloads (``options``, ``keep_alive``,
``think``, ``format``, ``messages[].images``) into clean OpenAI JSON requests
for Sonder Inference, and normalizes OpenAI ``choices[0].message`` /
``choices[0].text`` responses into the internal response dict expected by
``runtime/server.py`` callers (while passing through already-normalized dicts).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .sonder_inference_gateway import _encode_image_content_parts

_OPENAI_OPTION_MAP: tuple[tuple[str, str], ...] = (
    ("temperature", "temperature"),
    ("top_p", "top_p"),
    ("top_k", "top_k"),
    ("min_p", "min_p"),
    ("repeat_penalty", "repeat_penalty"),
    ("seed", "seed"),
    ("stop", "stop"),
    ("num_predict", "max_tokens"),
)

_PASSTHROUGH_CHAT_KEYS = (
    "model",
    "stream",
    "tools",
    "tool_choice",
    "response_format",
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "seed",
    "stop",
    "logprobs",
    "top_logprobs",
    "stream_options",
    "chat_template_kwargs",
    "reasoning_budget_tokens",
    "reasoning_budget_message",
    "prompt_cache_key",
)


def _translate_messages(raw_messages: object) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw_messages:
        if not isinstance(entry, Mapping):
            continue
        msg = dict(entry)
        raw_images = msg.pop("images", None)
        if raw_images:
            content = msg.get("content", "")
            msg["content"] = _encode_image_content_parts(
                content if isinstance(content, str) else str(content or ""),
                raw_images,
            )
        out.append(msg)
    return out


def to_openai_chat_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an Ollama-shaped chat payload into an OpenAI ``/v1/chat/completions`` body."""
    wire: dict[str, Any] = {}
    for key in _PASSTHROUGH_CHAT_KEYS:
        if key in payload and payload[key] is not None:
            wire[key] = payload[key]
    wire.setdefault("stream", False)
    if "messages" in payload:
        wire["messages"] = _translate_messages(payload.get("messages"))

    raw_options = payload.get("options")
    if isinstance(raw_options, Mapping):
        for src_key, dst_key in _OPENAI_OPTION_MAP:
            val = raw_options.get(src_key)
            if val is None or dst_key in wire:
                continue
            if dst_key == "max_tokens" and (isinstance(val, bool) or not isinstance(val, int) or val <= 0):
                continue
            wire[dst_key] = val

    raw_think = payload.get("think")
    if isinstance(raw_think, bool):
        existing = wire.get("chat_template_kwargs")
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        merged.setdefault("enable_thinking", raw_think)
        wire["chat_template_kwargs"] = merged

    raw_format = payload.get("format")
    if raw_format is not None and "response_format" not in wire:
        if raw_format == "json":
            wire["response_format"] = {"type": "json_object"}
        elif isinstance(raw_format, Mapping):
            wire["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": dict(raw_format)},
            }

    return wire


def to_openai_completion_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an Ollama-shaped generate payload into an OpenAI ``/v1/completions`` body."""
    wire: dict[str, Any] = {
        "model": payload.get("model", ""),
        "prompt": payload.get("prompt", ""),
        "stream": bool(payload.get("stream", False)),
    }
    raw_options = payload.get("options")
    if isinstance(raw_options, Mapping):
        for src_key, dst_key in _OPENAI_OPTION_MAP:
            val = raw_options.get(src_key)
            if val is None or dst_key in wire:
                continue
            if dst_key == "max_tokens" and (isinstance(val, bool) or not isinstance(val, int) or val <= 0):
                continue
            wire[dst_key] = val
    for key in ("max_tokens", "temperature", "top_p", "seed", "stop"):
        if key in payload and payload[key] is not None and key not in wire:
            wire[key] = payload[key]
    return wire


def from_openai_chat_response(data: Any) -> Any:
    """Normalize an OpenAI ``/v1/chat/completions`` response to expose ``message`` and ``done_reason``."""
    if not isinstance(data, dict):
        return data
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return data
    first = choices[0]
    if not isinstance(first, Mapping):
        return data
    out = dict(data)
    raw_msg = first.get("message")
    if isinstance(raw_msg, Mapping) and "message" not in out:
        msg: dict[str, Any] = dict(raw_msg)
        if "thinking" not in msg:
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                msg["thinking"] = reasoning
        out["message"] = msg
    finish = first.get("finish_reason")
    if finish is not None:
        out.setdefault("done_reason", finish)
    out.setdefault("done", True)
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        if isinstance(usage.get("prompt_tokens"), int):
            out.setdefault("prompt_eval_count", usage["prompt_tokens"])
        if isinstance(usage.get("completion_tokens"), int):
            out.setdefault("eval_count", usage["completion_tokens"])
    return out


def from_openai_completion_response(data: Any) -> Any:
    """Normalize an OpenAI ``/v1/completions`` response to expose ``response`` and ``done_reason``."""
    if not isinstance(data, dict):
        return data
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return data
    first = choices[0]
    if not isinstance(first, Mapping):
        return data
    out = dict(data)
    text = first.get("text")
    if isinstance(text, str):
        out.setdefault("response", text)
    finish = first.get("finish_reason")
    if finish is not None:
        out.setdefault("done_reason", finish)
    out.setdefault("done", True)
    return out


__all__ = [
    "from_openai_chat_response",
    "from_openai_completion_response",
    "to_openai_chat_payload",
    "to_openai_completion_payload",
]
