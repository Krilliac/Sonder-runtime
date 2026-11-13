"""The model gateway handed to lanes that call tools or apply model output.

The packaged ``ModelGateway`` serves text callers (chat, A2A, offloads,
replay) and tool or apply callers (interactive agent lanes, conversational
subagents, ``build_fix`` candidates) alike, and cannot tell them apart.  The
composition root therefore gives the tool and apply callers this wrapper:
before every request reaches the shared gateway it asks
``tool_lane_guard.gateway_refusal`` and raises ``Forbidden`` with the plain
refusal text when the request would be answered by a model on the tool-lane
deny list, or by a non-Ollama provider whose served model cannot be
determined, and after a non-Ollama reply it judges the model the reply
reports (``tool_lane_guard.reply_refusal``).  Text callers keep a gateway
whose target resolver answers a named blocked model as chat; ChatService
holds it through ``ServedTierGateway``, so a tier its provider serves with a
blocked model does not answer there either (a blocked model never serves a
tier).

Ownership: each wrapper holds references to the shared gateway (and the
target resolver) and the provider bindings; it owns none of them and keeps
no other state, so it is safe to share across threads exactly as the gateway
is.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sonder_runtime.adapters.tool_lane_guard import (
    gateway_refusal,
    reply_refusal,
    tier_serving_refusal,
)
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
        response = self._gateway.generate(request, context)
        refusal = reply_refusal(
            request.tier, (request.options or {}).get("model"), self._bindings,
            getattr(response, "model", None), text_only=False,
        )
        if refusal:
            raise Forbidden(refusal)
        return response

    def resolve_route(self, request: ModelRequest, context: OperationContext) -> Any:
        """Refuse a blocked route at classification time; ``None`` when unresolvable."""
        self._check(request)
        resolve = getattr(self._gateway, "resolve_route", None)
        return resolve(request, context) if callable(resolve) else None

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        return self._gateway.embed(texts, context)


class ServedTierGateway:
    """The text consumers' handle: a bridged tier never answers with a blocked model.

    A request for a tier (or the default ``sonder`` route) that the gateway
    sends to a non-Ollama provider is refused before sending when that
    provider is configured to serve a blocked model, and after the reply
    when the reply reports one; a request that names a model, an Ollama
    tier and an undetermined served model answer as text.  Every other
    attribute (``generate_strict_alias``, status reads) is the wrapped
    gateway's own.
    """

    def __init__(self, gateway: Any, *, bindings: Any) -> None:
        self._gateway = gateway
        self._bindings = bindings

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        explicit = (request.options or {}).get("model")
        refusal = tier_serving_refusal(request.tier, explicit, self._bindings)
        if refusal:
            raise Forbidden(refusal)
        response = self._gateway.generate(request, context)
        refusal = reply_refusal(
            request.tier, explicit, self._bindings, getattr(response, "model", None), text_only=True,
        )
        if refusal:
            raise Forbidden(refusal)
        return response

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this wrapper does not define; its own
        # fields are never delegated (no recursion before ``__init__`` ran).
        if name in ("_gateway", "_bindings") or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._gateway, name)


__all__ = ["ServedTierGateway", "ToolLaneGateway"]
