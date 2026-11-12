"""The model gateway handed to lanes that call tools or apply model output.

The packaged ``ModelGateway`` serves text callers (chat, A2A, offloads,
replay) and tool or apply callers (interactive agent lanes, conversational
subagents, ``build_fix`` candidates) alike, and cannot tell them apart.  The
composition root therefore gives the tool and apply callers this wrapper:
before every request reaches the shared gateway it asks
``tool_lane_guard.gateway_refusal`` and raises ``Forbidden`` with the plain
refusal text when the request would be answered by a model on the tool-lane
deny list, or by a non-Ollama provider whose served model cannot be
determined.  Text callers keep the unwrapped gateway, whose target resolver
answers a named blocked model as chat.

Ownership: the wrapper holds references to the shared gateway, the target
resolver and the provider bindings; it owns none of them and keeps no other
state, so it is safe to share across threads exactly as the gateway is.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sonder_runtime.adapters.tool_lane_guard import gateway_refusal
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
)
from sonder_runtime.domain.common.errors import Forbidden


class ToolLaneGateway:
    """A ``ModelGateway`` that refuses tool-lane-denied models before any call.

    Only ``generate``, ``resolve_route`` and ``embed`` are offered: any other
    generating entry point of the wrapped gateway is deliberately not
    reachable through this object.
    """

    def __init__(
        self,
        gateway: Any,
        *,
        resolve_target: Callable[..., Any],
        bindings: Any,
    ) -> None:
        self._gateway = gateway
        self._resolve_target = resolve_target
        self._bindings = bindings

    def _check(self, request: ModelRequest) -> None:
        explicit = (request.options or {}).get("model")
        refusal = gateway_refusal(
            request.tier, explicit, self._resolve_target, self._bindings,
        )
        if refusal:
            raise Forbidden(refusal)

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        self._check(request)
        return self._gateway.generate(request, context)

    def resolve_route(self, request: ModelRequest, context: OperationContext) -> Any:
        """Refuse a blocked route at classification time; ``None`` when unresolvable."""
        self._check(request)
        resolve = getattr(self._gateway, "resolve_route", None)
        return resolve(request, context) if callable(resolve) else None

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        return self._gateway.embed(texts, context)


__all__ = ["ToolLaneGateway"]
