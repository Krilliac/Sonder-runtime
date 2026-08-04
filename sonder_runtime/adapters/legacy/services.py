"""Legacy adapters: root-module implementations behind SPEC-3 ports.

The strangler strategy (SPEC-3 section 11) wraps the existing flat
modules as adapters first, then moves implementations behind the ports
use case by use case. Imports are deliberately lazy — building the
application graph must not drag in the 500KB server module until a
service is actually exercised.
"""
from __future__ import annotations

import time

from ...application.context import OperationContext
from ...application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
)


class LegacyPolicyRepository:
    """PolicyRepository over the root runtime_policy module."""

    def load(self) -> dict:
        import runtime_policy

        return runtime_policy.load()

    def update(
        self,
        *,
        local_models: dict | None = None,
        routing: dict | None = None,
        npu: dict | None = None,
        expected_revision: int | None = None,
        source: str = "application",
    ) -> dict:
        import runtime_policy

        return runtime_policy.update(
            local_models=local_models,
            routing=routing,
            npu=npu,
            source=source,
            expected_revision=expected_revision,
        )


class LegacyModelGateway:
    """ModelGateway over the root server module's chat entry point."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        import server

        started = time.monotonic()
        text = server.sonder(
            request.prompt,
            history=list(request.history) or None,
            tier=request.tier or None,
        )
        return ModelResponse(
            text=text,
            model=request.tier or "sonder",
            tier=request.tier or "general",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def embed(self, texts, context: OperationContext):
        import embeddings

        return [
            Embedding(vector=tuple(embeddings.embed(text) or ()), model="local")
            for text in texts
        ]


class OperationsEventSink:
    """EventSink over the SPEC-2 operations store."""

    def __init__(self) -> None:
        self._store = None

    def emit(
        self,
        event_code: str,
        *,
        summary: str,
        detail: dict | None = None,
        severity: str = "INFO",
        correlation_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        if self._store is None:
            from sonder_operations_store import OperationsStore

            self._store = OperationsStore()
        self._store.record_event(
            component="application",
            event_code=event_code,
            severity=severity,
            summary=summary,
            detail=detail,
            correlation_id=correlation_id,
            operation_id=operation_id,
        )


class SystemClock:
    def now_utc_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def monotonic(self) -> float:
        return time.monotonic()
