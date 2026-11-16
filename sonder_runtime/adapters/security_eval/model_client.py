"""Ask a named local model for a verdict through the Sonder model gateway, residency first.

Residency is checked before any request is sent, and only ever to refuse: a route that may leave the
machine (a hosted/cloud tier, or a gateway that cannot classify its routes) raises ``ResidencyRefused``
and nothing is sent. A worker-pool route needs the operator's remote-Ollama consent, which travels in
the operation context (the gateway refuses without it). Source text, hypotheses and context only ever
go to this one local model.

This is the one ``.generate(request, context)`` site of the eval. The reply is text that is parsed and
scored, never run, so the registrar hands it a text-only gateway (ServedTierGateway).
"""
from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any, Mapping, cast

from ...application.context import OperationContext, local_owner_context
from ...application.ports.model_gateway import ModelRequest, require_model_text
from ...application.security_eval.ports import (
    SERVED_MODEL_SOURCE, TOKEN_MEASURED, TOKEN_UNVERIFIED, ModelCall)
from ...domain.common.errors import Forbidden
from ...domain.model_routing import is_cloud_model_name
from ...domain.security_eval.errors import ResidencyRefused

_COUNTER = itertools.count(1)


def _lexically_cloud(name: str) -> bool:
    lowered = name.casefold()
    return is_cloud_model_name(name) or lowered.startswith("cloud") or lowered.endswith(":cloud")


class GatewayVerdictModel:
    """``VerdictModel`` over a ``ModelGateway``: one explicit-system request per call, sequential."""

    def __init__(self, gateway: Any, model: str, *, options: Mapping[str, object] | None = None,
                 allow_remote_ollama: bool = False, item_timeout_seconds: float = 300.0) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("a model name is required")
        if not callable(getattr(gateway, "generate", None)):
            raise TypeError("gateway must offer generate(request, context)")
        self._gateway = gateway
        self._model = model.strip()
        self._options = dict(options or {})
        self._allow_remote = bool(allow_remote_ollama)
        self._timeout = float(item_timeout_seconds)
        self._checked = False

    def _context(self) -> OperationContext:
        return local_owner_context(
            correlation_id="security-eval-%d" % next(_COUNTER), source="mcp", cloud_allowed=False,
            remote_ollama_allowed=self._allow_remote, timeout_seconds=self._timeout)

    def _resolver(self) -> Callable[..., Any] | None:
        for candidate in (self._gateway, getattr(self._gateway, "gateway", None)):
            resolve = getattr(candidate, "resolve_route", None)
            if callable(resolve):
                return cast("Callable[..., Any]", resolve)
        return None

    def preflight(self) -> None:
        """Refuse a route that may leave the machine; passes at most once per instance."""
        if self._checked:
            return
        if _lexically_cloud(self._model):
            raise ResidencyRefused("model %r looks like a hosted tier; nothing was sent" % self._model)
        resolve = self._resolver()
        if resolve is None:
            raise ResidencyRefused("the model gateway cannot classify routes, so residency is unknown; "
                                   "nothing was sent")
        try:
            route = resolve(ModelRequest("Classify security-eval route residency.", tier=self._model),
                            self._context())
        except Forbidden as exc:
            raise ResidencyRefused(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - an unclassifiable route fails closed
            raise ResidencyRefused("the route could not be classified (%s); nothing was sent"
                                   % type(exc).__name__) from None
        cloud = getattr(route, "cloud", None)
        if type(cloud) is not bool:
            raise ResidencyRefused("the route has no residency classification; nothing was sent")
        if cloud:
            raise ResidencyRefused("route %r may leave this machine; the security eval never sends "
                                   "source text to a hosted model" % self._model)
        self._checked = True

    def ask(self, system: str, user: str) -> ModelCall:
        self.preflight()
        if not system or not system.strip():
            raise ValueError("an explicit system prompt is required")
        request = ModelRequest(
            prompt=user, tier=self._model, system=system, options=dict(self._options),
            routing_metadata={"purpose": "security_eval", "tools": "none"})
        response = self._gateway.generate(request, self._context())
        text = require_model_text(getattr(response, "text", None))
        telemetry = getattr(response, "telemetry", None)
        measured_in = getattr(telemetry, "prompt_tokens", None)
        measured_out = getattr(telemetry, "output_tokens", None)
        tokens_in: int | None
        tokens_out: int | None
        if isinstance(measured_in, int) and isinstance(measured_out, int):
            tokens_in, tokens_out, source = measured_in, measured_out, TOKEN_MEASURED
        else:
            tokens_in, tokens_out, source = (getattr(response, "tokens_in", None),
                                             getattr(response, "tokens_out", None), TOKEN_UNVERIFIED)
        served = str(getattr(response, "model", "") or "")
        return ModelCall(text=text, requested_model=self._model, served_model=served,
                         served_model_source=SERVED_MODEL_SOURCE, tokens_in=tokens_in,
                         tokens_out=tokens_out, token_source=source,
                         duration_ms=int(getattr(response, "duration_ms", 0) or 0))
