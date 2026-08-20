"""Concrete SEAM-014 lifecycle adapters and registry wiring.

The specialized ports are intentionally provider-neutral.  This module is the
composition-boundary implementation: callers inject an embedding callable,
training backend, and update activator, and this module publishes them as
normal :class:`ScopedProviderRegistry` providers.  It does not import the
legacy server or select a backend.

Every adapter has the same lifecycle guarantees:

* operations are admitted only while the provider is published and healthy;
* cancellation is cooperative and is visible to delegates through a wrapped
  cancellation token;
* cleanup waits for admitted operations to quiesce within its deadline; and
* a failed multi-provider publication unregisters already-published providers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, Event, RLock
import time
from typing import Any, Callable

from ..context import OperationContext
from ..ports.model_gateway import Embedding, require_embedding_vector
from ..ports.specialized_lifecycle import (
    ActivationRequest,
    ActivationResult,
    CleanupResult,
    DeploymentResult,
    EmbeddingRequest,
    EmbeddingResult,
    HealthReport,
    HealthStatus,
    TrainingRequest,
)
from .lifecycle_registry import (
    ProviderLifecycleError,
    ProviderRegistration,
    ProviderRegistrationScope,
    ScopedProviderRegistry,
)


class SpecializedLifecycleError(RuntimeError):
    """Raised when a specialized provider cannot be admitted or invoked."""


class _State(StrEnum):
    NEW = "new"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"


class _Cancellation:
    """Cancellation view combining the caller token and adapter shutdown."""

    def __init__(self, caller: Any, shutdown: Event) -> None:
        self._caller = caller
        self._shutdown = shutdown

    @property
    def cancelled(self) -> bool:
        return bool(self._shutdown.is_set() or getattr(self._caller, "cancelled", False))

    def wait(self, timeout: float | None = None) -> bool:
        if self._shutdown.wait(timeout=0 if timeout is None else timeout):
            return True
        waiter = getattr(self._caller, "wait", None)
        return bool(waiter(timeout=0) if callable(waiter) else False)


class _LifecycleAdapter:
    """Thread-safe lifecycle shell shared by the three concrete adapters."""

    capability_name = ""

    def __init__(self, provider_id: str, *, on_initialize: Callable[[], None] | None = None,
                 on_cleanup: Callable[[], None] | None = None) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        self.provider_id = provider_id.strip()
        self._on_initialize = on_initialize
        self._on_cleanup = on_cleanup
        self._state = _State.NEW
        self._active = 0
        self._cancel = Event()
        self._condition = Condition(RLock())

    def initialize(self, scope: ProviderRegistrationScope) -> None:
        with self._condition:
            if self._state is not _State.NEW:
                raise SpecializedLifecycleError("provider has already been initialized")
            if self._on_initialize is not None:
                self._on_initialize()
            scope.register(self.capability_name, self)
            self._state = _State.READY

    def health(self) -> HealthReport:
        with self._condition:
            state = self._state
            active = self._active
        status = HealthStatus.HEALTHY if state is _State.READY else (
            HealthStatus.DEGRADED if state is _State.CLOSING else HealthStatus.UNHEALTHY
        )
        return HealthReport(
            provider_id=self.provider_id,
            status=status,
            detail=f"state={state.value}; active_operations={active}",
        )

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        del reason  # The token is the durable cancellation signal; no secret is logged.
        with self._condition:
            if self._state is _State.CLOSED:
                return False
            had_work = self._active > 0 or self._state is _State.CLOSING
            self._cancel.set()
            self._condition.notify_all()
            return had_work

    def cleanup(self, timeout: float | None = None) -> CleanupResult:
        limit = None if timeout is None else max(0.0, float(timeout))
        deadline = None if limit is None else time.monotonic() + limit
        with self._condition:
            if self._state is _State.CLOSED:
                return CleanupResult(self.provider_id, True, True, "already closed")
            self._state = _State.CLOSING
            self._cancel.set()
            while self._active:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0.0:
                    return CleanupResult(self.provider_id, False, False, "active operations remain")
                self._condition.wait(remaining)
            callback = self._on_cleanup
            try:
                if callback is not None:
                    callback()
            except Exception as exc:
                return CleanupResult(self.provider_id, False, False, f"cleanup failed: {type(exc).__name__}")
            self._state = _State.CLOSED
            return CleanupResult(self.provider_id, True, True, "resources released")

    def _enter(self, context: OperationContext) -> _Cancellation:
        if context.expired or context.cancellation.cancelled:
            raise SpecializedLifecycleError("operation cancelled or deadline expired")
        with self._condition:
            if self._state is not _State.READY:
                raise SpecializedLifecycleError("provider is not ready")
            self._active += 1
            return _Cancellation(context.cancellation, self._cancel)

    def _leave(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    @staticmethod
    def _context(context: OperationContext, cancellation: _Cancellation) -> OperationContext:
        return OperationContext(
            correlation_id=context.correlation_id,
            principal_id=context.principal_id,
            auth_level=context.auth_level,
            source=context.source,
            deadline_monotonic=context.deadline_monotonic,
            cancellation=cancellation,
            workspace_roots=context.workspace_roots,
            cloud_allowed=context.cloud_allowed,
            remote_ollama_allowed=context.remote_ollama_allowed,
        )

    @staticmethod
    def _call(delegate: Any, request: Any, context: OperationContext) -> Any:
        operation = getattr(delegate, "embed", None) or getattr(delegate, "train", None) or getattr(delegate, "activate", None)
        if not callable(operation):
            operation = delegate
        if not callable(operation):
            raise SpecializedLifecycleError("specialized delegate is not callable")
        return operation(request, context)


class EmbeddingLifecycleAdapter(_LifecycleAdapter):
    """Adapt an injected embedding port/callable into a registry provider."""

    capability_name = "embedding"

    def __init__(self, embedder: Any, *, provider_id: str = "embedding") -> None:
        super().__init__(provider_id)
        self._embedder = embedder

    def embed(self, request: EmbeddingRequest, context: OperationContext) -> EmbeddingResult:
        if not request.texts or any(not isinstance(text, str) for text in request.texts):
            raise SpecializedLifecycleError("embedding request must contain text")
        token = self._enter(context)
        try:
            delegated = self._call(self._embedder, request, self._context(context, token))
            if isinstance(delegated, EmbeddingResult):
                if delegated.provider_id != self.provider_id:
                    return EmbeddingResult(self.provider_id, delegated.model, delegated.embeddings)
                return delegated
            vectors = delegated if isinstance(delegated, (list, tuple)) else None
            if vectors is None or len(vectors) != len(request.texts):
                raise SpecializedLifecycleError("embedding delegate returned an invalid result")
            embeddings = tuple(
                item if isinstance(item, Embedding) else Embedding(require_embedding_vector(item), request.model)
                for item in vectors
            )
            return EmbeddingResult(self.provider_id, request.model, embeddings)
        finally:
            self._leave()


class TrainingLifecycleAdapter(_LifecycleAdapter):
    """Adapt an injected attended training backend into a registry provider."""

    capability_name = "training"

    def __init__(self, backend: Any, *, provider_id: str = "training") -> None:
        super().__init__(provider_id)
        self._backend = backend

    def train(self, request: TrainingRequest, context: OperationContext) -> DeploymentResult:
        for value in (request.run_id, request.base_model, request.base_revision, request.dataset_digest):
            if not isinstance(value, str) or not value.strip():
                raise SpecializedLifecycleError("training request fields must be non-empty")
        token = self._enter(context)
        try:
            result = self._call(self._backend, request, self._context(context, token))
            if not isinstance(result, DeploymentResult):
                raise SpecializedLifecycleError("training delegate returned an invalid result")
            return result if result.provider_id == self.provider_id else DeploymentResult(
                self.provider_id, result.run_id, result.deployment_id, result.model_id,
                result.artifact_digest, result.created_at,
            )
        finally:
            self._leave()


class UpdateLifecycleAdapter(_LifecycleAdapter):
    """Adapt an injected verified update activator into a registry provider."""

    capability_name = "update"

    def __init__(self, activator: Any, *, provider_id: str = "update") -> None:
        super().__init__(provider_id)
        self._activator = activator

    def activate(self, request: ActivationRequest, context: OperationContext) -> ActivationResult:
        for value in (request.activation_id, request.release_id, request.version, request.artifact_digest):
            if not isinstance(value, str) or not value.strip():
                raise SpecializedLifecycleError("activation request fields must be non-empty")
        token = self._enter(context)
        try:
            result = self._call(self._activator, request, self._context(context, token))
            if not isinstance(result, ActivationResult):
                raise SpecializedLifecycleError("update delegate returned an invalid result")
            return result if result.provider_id == self.provider_id else ActivationResult(
                self.provider_id, result.activation_id, result.release_id, result.version,
                result.artifact_digest, result.activated_at, result.previous_version,
            )
        finally:
            self._leave()


@dataclass(frozen=True, slots=True)
class SpecializedProviderBundle:
    """Published registrations with an idempotent bundle close operation."""

    registry: ScopedProviderRegistry
    registrations: tuple[ProviderRegistration, ...]

    def close(self, timeout: float | None = None) -> None:
        for registration in reversed(self.registrations):
            try:
                self.registry.unregister(registration.provider_id, timeout=timeout)
            except ProviderLifecycleError:
                # A failed close is not silently converted to success; callers
                # get a deterministic lifecycle error from the registry.
                raise


def wire_specialized_providers(
    registry: ScopedProviderRegistry,
    *,
    embedding: EmbeddingLifecycleAdapter,
    training: TrainingLifecycleAdapter,
    update: UpdateLifecycleAdapter,
) -> SpecializedProviderBundle:
    """Publish all three specialized providers as one composition operation.

    If any provider cannot initialize or its capability conflicts, providers
    already published by this call are synchronously removed.  The caller's
    pre-existing registry state is therefore restored exactly on failure.
    """
    candidates = (embedding, training, update)
    published: list[ProviderRegistration] = []
    try:
        for provider in candidates:
            published.append(registry.register(provider))
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        for registration in reversed(published):
            try:
                registry.unregister(registration.provider_id, timeout=0)
            except BaseException as rollback_exc:
                cleanup_error = rollback_exc
                break
        if cleanup_error is not None:
            raise SpecializedLifecycleError("specialized provider publication rollback failed") from cleanup_error
        raise exc
    return SpecializedProviderBundle(registry, tuple(published))


__all__ = [
    "EmbeddingLifecycleAdapter",
    "SpecializedLifecycleError",
    "SpecializedProviderBundle",
    "TrainingLifecycleAdapter",
    "UpdateLifecycleAdapter",
    "wire_specialized_providers",
]
