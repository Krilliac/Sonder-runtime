"""Small, transport-independent OpenAI-compatible envelope boundary.

The HTTP interface owns authentication, host policy, streaming, and network
errors.  This module only translates the supported text-only subset of the
Chat Completions and Responses envelopes into one canonical shape.  Hooks are
deliberately injected so callers can retain their existing policy and event
recording without making this boundary depend on HTTP or an event store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class CompatibilityError(ValueError):
    """A request or provider response is outside the supported contract."""


@dataclass(frozen=True)
class CanonicalRequest:
    operation: str
    model: str
    messages: tuple[dict[str, str], ...]
    stream: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalResponse:
    operation: str
    response_id: str
    model: str
    text: str
    finish_reason: str = "stop"
    usage: Mapping[str, int] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


PolicyHook = Callable[[str, Mapping[str, Any]], Any]
EventHook = Callable[[Mapping[str, Any]], Any]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompatibilityError("%s must be a non-empty string" % label)
    return value


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError("%s must be an object" % label)
    return value


def _messages(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise CompatibilityError("messages must be a non-empty array")
    supported = {"system", "user", "assistant"}
    result = []
    for index, raw in enumerate(value):
        item = _object(raw, "messages[%d]" % index)
        role = item.get("role")
        if role not in supported:
            raise CompatibilityError(
                "messages[%d].role must be system, user, or assistant" % index
            )
        result.append({"role": role, "content": _text(item.get("content"),
                                                          "messages[%d].content" % index)})
    if not any(item["role"] == "user" for item in result):
        raise CompatibilityError("messages must contain a user message")
    return tuple(result)


def _responses_messages(value: Any) -> tuple[dict[str, str], ...]:
    if isinstance(value, str):
        return ({"role": "user", "content": _text(value, "input")},)
    if not isinstance(value, list) or not value:
        raise CompatibilityError("input must be a string or non-empty array")
    messages = []
    for index, raw in enumerate(value):
        item = _object(raw, "input[%d]" % index)
        role = item.get("role", "user")
        content = item.get("content")
        # Support the common Responses input_text part, but not multimodal or
        # tool parts.  This keeps the boundary honest about its text-only scope.
        if isinstance(content, list):
            texts = []
            for part in content:
                part_obj = _object(part, "input[%d].content" % index)
                if part_obj.get("type") != "input_text":
                    raise CompatibilityError("only input_text content is supported")
                texts.append(_text(part_obj.get("text"), "input_text.text"))
            content = "".join(texts)
        if role not in {"system", "user", "assistant"}:
            raise CompatibilityError("input[%d].role is unsupported" % index)
        messages.append({"role": role, "content": _text(content, "input[%d].content" % index)})
    return _messages(messages)


class OpenAICompatibility:
    """Normalize supported envelopes while retaining host-owned hooks."""

    def __init__(self, *, policy_hook: PolicyHook | None = None,
                 event_hook: EventHook | None = None) -> None:
        self._policy_hook = policy_hook
        self._event_hook = event_hook

    def _authorize(self, operation: str, payload: Mapping[str, Any]) -> None:
        if self._policy_hook is not None and self._policy_hook(operation, payload) is False:
            raise CompatibilityError("request rejected by policy")

    def _event(self, event: Mapping[str, Any]) -> None:
        if self._event_hook is not None:
            self._event_hook(dict(event))

    def request(self, payload: Mapping[str, Any], *, operation: str) -> CanonicalRequest:
        if operation not in {"chat.completions", "responses"}:
            raise CompatibilityError("unsupported operation")
        payload = _object(payload, "request")
        self._authorize(operation, payload)
        model = _text(payload.get("model"), "model")
        if operation == "chat.completions":
            messages = _messages(payload.get("messages"))
        else:
            messages = _responses_messages(payload.get("input"))
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise CompatibilityError("stream must be a boolean")
        reserved = {"model", "messages", "input", "stream"}
        options = {key: value for key, value in payload.items() if key not in reserved}
        result = CanonicalRequest(operation, model, messages, stream, options,
                                  payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {})
        self._event({"kind": "openai.request.normalized", "operation": operation,
                     "model": model, "stream": stream})
        return result

    def response(self, payload: Mapping[str, Any], *, operation: str) -> CanonicalResponse:
        if operation not in {"chat.completions", "responses"}:
            raise CompatibilityError("unsupported operation")
        payload = _object(payload, "response")
        if operation == "chat.completions":
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise CompatibilityError("choices must be a non-empty array")
            choice = _object(choices[0], "choices[0]")
            message = _object(choice.get("message"), "choices[0].message")
            text = _text(message.get("content"), "choices[0].message.content")
            finish = choice.get("finish_reason") or "stop"
            response_id = _text(payload.get("id"), "id")
        else:
            text = payload.get("output_text")
            if not isinstance(text, str):
                output = payload.get("output")
                if isinstance(output, list):
                    parts = []
                    for item in output:
                        obj = _object(item, "output item")
                        for content in obj.get("content", []):
                            content_obj = _object(content, "output content")
                            if content_obj.get("type") == "output_text":
                                parts.append(_text(content_obj.get("text"), "output_text.text"))
                    text = "".join(parts)
            text = _text(text, "output_text")
            finish = payload.get("status") or "completed"
            response_id = _text(payload.get("id"), "id")
        usage = payload.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise CompatibilityError("usage must be an object")
        result = CanonicalResponse(operation, response_id,
                                   _text(payload.get("model"), "model"), text,
                                   str(finish), dict(usage))
        self._event({"kind": "openai.response.normalized", "operation": operation,
                     "model": result.model, "response_id": response_id})
        return result

    @staticmethod
    def render(response: CanonicalResponse, *, operation: str | None = None) -> dict[str, Any]:
        operation = operation or response.operation
        if operation == "chat.completions":
            return {"id": response.response_id, "object": "chat.completion",
                    "model": response.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": response.text},
                                  "finish_reason": response.finish_reason}],
                    "usage": dict(response.usage)}
        if operation == "responses":
            return {"id": response.response_id, "object": "response", "model": response.model,
                    "status": "completed", "output_text": response.text,
                    "output": [{"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": response.text}]}],
                    "usage": dict(response.usage)}
        raise CompatibilityError("unsupported operation")
