"""Strangler adapters: root-module implementations behind SPEC-3 ports.

The strangler strategy (SPEC-3 section 11) wraps the existing flat
modules as adapters first, then moves implementations behind the ports
use case by use case. Imports are deliberately lazy — building the
application graph must not drag in the 500KB server module until a
service is actually exercised.
"""
from __future__ import annotations

import time

from .persistence.autopilot_repository import AutopilotRepository
from .runtime_policy_repository import RuntimePolicyRepository
from .memory_repository import MemoryRepositoryAdapter
from ..application.context import OperationContext
from ..application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    require_embedding_vector,
    require_model_text,
)
from .tool_executor import ToolExecutorAdapter
from .operations_event_sink import OperationsEventSink
from ..domain.common.errors import Cancelled, DeadlineExceeded, InvalidInput


def _check_context_liveness(context: OperationContext) -> None:
    if context.expired:
        raise DeadlineExceeded("operation deadline exceeded before adapter call")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled("operation cancelled before adapter call")


class LegacyUnitOfWork:
    """UnitOfWork owning one memory-store connection for a transaction scope.

    Opens the canonical memory database on ``__enter__`` and exposes the
    memory repository bound to that connection; the automation and policy
    repositories are connection-independent (they manage their own stores),
    and events go to the SPEC-2 operations store.

    Honest boundary note: several root ``memory_store`` operations (e.g.
    ``add_fact``, ``log_interaction``) still self-commit, so ``rollback`` does
    not yet undo them. The UnitOfWork owns the connection lifecycle today; the
    transaction boundary tightens as those ops migrate off self-commit.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn = None
        self.memory = None
        self.automation = AutopilotRepository()
        self.policy = RuntimePolicyRepository()
        self.events = OperationsEventSink()

    def __enter__(self) -> "LegacyUnitOfWork":
        import sonder_runtime.adapters.memory_store as memory_store
        from sonder_runtime.platform import paths

        path = self._db_path or paths.memory_db_path()
        self._conn = memory_store.connect(path)
        self.memory = MemoryRepositoryAdapter(self._conn)
        return self

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self.memory = None


class LegacyModelGateway:
    """ModelGateway over the root server module's chat entry point."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        import server

        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        _check_context_liveness(context)
        started = time.monotonic()
        text = server.sonder(
            request.prompt,
            history=list(request.history) or None,
            tier=request.tier or None,
        )
        return ModelResponse(
            text=require_model_text(text),
            model=request.tier or "sonder",
            tier=request.tier or "general",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def embed(self, texts, context: OperationContext):
        import sonder_runtime.adapters.embeddings as embeddings

        results = []
        for text in texts:
            _check_context_liveness(context)
            results.append(Embedding(
                vector=require_embedding_vector(embeddings.embed(text)),
                model="local",
            ))
        return results


# Compatibility name for callers that still import the pre-migration adapter.
LegacyMemoryRepository = MemoryRepositoryAdapter
LegacyToolExecutor = ToolExecutorAdapter
