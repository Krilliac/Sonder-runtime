"""Fail-closed pre-send fallback from Sonder Inference to local Ollama.

Only a request that *provably did not execute* on the primary may be sent to
the fallback, and only once.  "Provably" is decided by the primary: it raises
:class:`SonderInferenceUnreachable` for connection refused, an unresolvable
host, cached health that is not ready, or HTTP 503 ``not_ready`` -- nothing
else.  Timeouts, 4xx, other 5xx, cancellation and capacity refusals propagate
unchanged, because the primary may have run the request and a second
execution would double any effect and any metered cost.

The fallback target keeps its own consent rules (the Ollama gateway never
sends to the cloud), so this wrapper can never widen where a prompt goes.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from ...domain.common.errors import Cancelled, DeadlineExceeded
from ..inference.sonder_inference_gateway import SonderInferenceUnreachable

logger = logging.getLogger(__name__)

FALLBACK_REASON_CODE = "primary_unreachable"

# ``observer(from_provider, to_provider, reason_code, context)`` is called once
# per fallback, before the fallback send.  It is the seam telemetry uses to
# emit ``route.changed`` for a refusal that never reached dispatch_provider.
FallbackObserver = Callable[[str, str, str, OperationContext], None]


class PreSendFallbackGateway:
    """Wrap one primary gateway with a single, pre-send-only fallback."""

    def __init__(
        self,
        primary: ModelGateway,
        *,
        fallback: ModelGateway,
        primary_id: str = "sonder_inference",
        fallback_id: str = "ollama",
        observer: FallbackObserver | None = None,
    ) -> None:
        if primary is fallback:
            raise ValueError("fallback gateway must differ from the primary")
        self._primary = primary
        self._fallback = fallback
        self._primary_id = primary_id
        self._fallback_id = fallback_id
        self._observer = observer
        self._lock = threading.Lock()
        self._fallback_count = 0

    @property
    def primary(self) -> ModelGateway:
        return self._primary

    @property
    def fallback(self) -> ModelGateway:
        return self._fallback

    @property
    def fallback_count(self) -> int:
        with self._lock:
            return self._fallback_count

    # Capability, admission and route metadata describe the primary: the
    # fallback is an outage path, not a second advertised provider.
    @property
    def capabilities(self):
        return getattr(self._primary, "capabilities", frozenset())

    @property
    def request_admission(self):
        return getattr(self._primary, "request_admission")

    def resolve_route(self, request: ModelRequest, context: OperationContext):
        resolver = getattr(self._primary, "resolve_route", None)
        return resolver(request, context) if callable(resolver) else None

    def capability_health(self):
        return self._primary.capability_health()

    def backend_identity(self, model: str | None = None):
        return self._primary.backend_identity(model)

    def routing_identity(self, model: str | None = None):
        return self._primary.routing_identity(model)

    def provider_status(self) -> Mapping[str, Mapping[str, object]]:
        reporter = getattr(self._primary, "provider_status", None)
        if callable(reporter):
            status = {key: dict(value) for key, value in reporter().items()}
        else:
            status = {self._primary_id: {"provider": self._primary_id, "state": "unknown"}}
        entry = status.setdefault(
            self._primary_id, {"provider": self._primary_id, "state": "unknown"},
        )
        entry["fallback"] = self._fallback_id
        entry["fallback_count"] = self.fallback_count
        return status

    def generate(self, request: ModelRequest, context: OperationContext) -> ModelResponse:
        try:
            return self._primary.generate(request, context)
        except SonderInferenceUnreachable as exc:
            # The primary never executed the request, but the caller may have
            # given up meanwhile; never start new work for a dead operation.
            if context.cancellation is not None and context.cancellation.cancelled:
                raise Cancelled("operation cancelled before provider fallback") from exc
            if context.expired:
                raise DeadlineExceeded("operation deadline exceeded before provider fallback") from exc
            with self._lock:
                self._fallback_count += 1
                count = self._fallback_count
            logger.warning(
                "provider fallback %s -> %s (count=%d, correlation_id=%s): %s",
                self._primary_id, self._fallback_id, count,
                context.correlation_id, exc,
            )
            if self._observer is not None:
                try:
                    self._observer(
                        self._primary_id, self._fallback_id, FALLBACK_REASON_CODE, context,
                    )
                except Exception:  # noqa: BLE001 - telemetry never fails a turn
                    logger.exception("provider fallback observer failed")
            return self._fallback.generate(request, context)

    def embed(self, texts: Sequence[str], context: OperationContext) -> Sequence[Embedding]:
        # Embeddings have their own binding; the fallback covers generation.
        return self._primary.embed(texts, context)


__all__ = ["FALLBACK_REASON_CODE", "FallbackObserver", "PreSendFallbackGateway"]
