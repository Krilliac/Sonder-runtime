"""Root-free HTTP model-request facade.

This boundary owns only the bounded OpenAI-compatible model request family:
``/v1/chat/completions`` and ``/v1/responses``.  It deliberately does not own
authentication, sockets, streaming writes, model discovery, or control/admin
dispatch.  Those policies remain injected by the HTTP adapter.

The facade translates protocol envelopes to the existing application
``ModelRequest``/``ModelGateway`` contracts and reports normalization through
the existing event hook on :class:`OpenAICompatibility`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
import time
import uuid

from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.model_gateway import (
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from sonder_runtime.application.protocol.openai_compatibility import (
    CanonicalRequest,
    CanonicalResponse,
    CompatibilityError,
    EventHook,
    OpenAICompatibility,
    PolicyHook,
)


MODEL_ROUTES = {
    "/v1/chat/completions": "chat.completions",
    "/v1/responses": "responses",
}
MAX_MESSAGES = 128
MAX_MESSAGE_CHARS = 1_048_576
MAX_OPTIONS = 64


class ModelFacadeError(ValueError):
    """A bounded protocol failure safe to translate into an HTTP 400."""

    status = 400
    error_type = "invalid_request_error"


class ModelRequestPort(Protocol):
    """Minimal injected generation port used by the facade."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class ModelRoute:
    path: str
    operation: str


@dataclass(frozen=True)
class ModelInvocation:
    """Normalized request plus the provider-neutral response."""

    request: CanonicalRequest
    response: CanonicalResponse

    def render(self) -> dict[str, Any]:
        return OpenAICompatibility.render(self.response)


def _path(value: object) -> str:
    # The HTTP adapter passes a path, not a full URL.  Keeping this helper
    # dependency-free makes direct facade use safe in tests and other adapters.
    text = str(value or "")
    if "?" in text:
        text = text.split("?", 1)[0]
    return text.rstrip("/") or "/"


def _bounded_request(payload: Mapping[str, Any]) -> None:
    messages = payload.get("messages")
    if messages is None:
        messages = payload.get("input")
    if isinstance(messages, list) and len(messages) > MAX_MESSAGES:
        raise ModelFacadeError("message count exceeds the HTTP bound")
    encoded_size = sum(
        len(str(item.get("content", "")))
        if isinstance(item, Mapping) else 0
        for item in (messages or [])
    ) if isinstance(messages, list) else (
        len(messages) if isinstance(messages, str) else 0
    )
    if encoded_size > MAX_MESSAGE_CHARS:
        raise ModelFacadeError("message content exceeds the HTTP bound")
    if len(payload) > MAX_OPTIONS + 8:
        raise ModelFacadeError("request contains too many fields")


class ModelRequestFacade:
    """Classify, normalize, invoke, and render the bounded model route family."""

    def __init__(self, *, policy_hook: PolicyHook | None = None,
                 event_hook: EventHook | None = None) -> None:
        self._event_hook = event_hook
        self._compatibility = OpenAICompatibility(
            policy_hook=policy_hook, event_hook=event_hook,
        )

    def route(self, path: object) -> ModelRoute | None:
        normalized = _path(path)
        operation = MODEL_ROUTES.get(normalized)
        return ModelRoute(normalized, operation) if operation else None

    def normalize(self, path: object, payload: Mapping[str, Any]) -> CanonicalRequest:
        route = self.route(path)
        if route is None:
            raise ModelFacadeError("unsupported model route")
        if not isinstance(payload, Mapping):
            raise ModelFacadeError("request must be an object")
        try:
            _bounded_request(payload)
            request = self._compatibility.request(payload, operation=route.operation)
        except ModelFacadeError:
            raise
        except CompatibilityError as exc:
            raise ModelFacadeError(str(exc)) from exc
        if len(request.options) > MAX_OPTIONS:
            raise ModelFacadeError("request options exceed the HTTP bound")
        if request.operation == "responses" and request.stream:
            raise ModelFacadeError(
                "streaming is not enabled for the bounded Responses route"
            )
        return request

    @staticmethod
    def to_model_request(request: CanonicalRequest) -> ModelRequest:
        if not request.messages:
            raise ModelFacadeError("request has no messages")
        prompt = request.messages[-1]["content"]
        history = tuple(
            (message["role"], message["content"])
            for message in request.messages[:-1]
            if message["role"] in {"user", "assistant"}
        )
        system = "\n".join(
            message["content"]
            for message in request.messages
            if message["role"] == "system"
        )
        return ModelRequest(
            prompt=prompt,
            tier=request.model,
            system=system,
            history=history,
            options=dict(request.options),
            stream=request.stream,
        )

    def invoke(
        self,
        path: object,
        payload: Mapping[str, Any],
        gateway: ModelRequestPort,
        context: OperationContext,
    ) -> ModelInvocation:
        request = self.normalize(path, payload)
        started = time.monotonic()
        response = gateway.generate(self.to_model_request(request), context)
        if not isinstance(response, ModelResponse):
            raise ModelFacadeError("model gateway returned an invalid response")
        text = response.text
        if not isinstance(text, str) or not text.strip():
            raise ModelFacadeError("model gateway returned empty text")
        usage = {}
        if response.tokens_in is not None:
            usage["prompt_tokens"] = response.tokens_in
        if response.tokens_out is not None:
            usage["completion_tokens"] = response.tokens_out
        if usage:
            usage["total_tokens"] = sum(usage.values())
        canonical = CanonicalResponse(
            operation=request.operation,
            response_id="%s-%s" % (
                "chatcmpl" if request.operation == "chat.completions" else "resp",
                uuid.uuid4().hex[:16],
            ),
            model=response.model or request.model,
            text=text,
            usage=usage,
            metadata={"tier": response.tier, "duration_ms": max(
                response.duration_ms, int((time.monotonic() - started) * 1000)
            )},
        )
        if self._event_hook is not None:
            self._event_hook({
                "kind": "model.response.completed",
                "operation": request.operation,
                "model": canonical.model,
                "response_id": canonical.response_id,
            })
        return ModelInvocation(request, canonical)

    @staticmethod
    def render_text(operation: str, text: str, model: str) -> dict[str, Any]:
        """Render an already-executed bounded response through the shared codec."""
        if operation not in set(MODEL_ROUTES.values()):
            raise ModelFacadeError("unsupported model operation")
        if not isinstance(text, str) or not text.strip():
            raise ModelFacadeError("model response text is empty")
        return OpenAICompatibility.render(CanonicalResponse(
            operation=operation,
            response_id=("chatcmpl" if operation == "chat.completions" else "resp")
            + "-" + uuid.uuid4().hex[:16],
            model=str(model or "sonder"),
            text=text,
        ))


__all__ = [
    "MODEL_ROUTES", "MAX_MESSAGES", "MAX_MESSAGE_CHARS", "MAX_OPTIONS",
    "ModelFacadeError", "ModelInvocation", "ModelRequestFacade", "ModelRoute",
    "ModelRequestPort",
]
