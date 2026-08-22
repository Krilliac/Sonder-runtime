"""Provider-neutral contracts for specialized model lifecycles (WP3 SEAM-014).

This module is deliberately a port only.  It does not select an embedding
model, start training, verify an update, or activate a release.  Concrete
adapters own those concerns and return immutable evidence across this
boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..context import OperationContext
from .model_gateway import Embedding


class HealthStatus(StrEnum):
    """Provider health as observed by the application boundary."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Immutable health evidence; ``detail`` is safe for operator display."""

    provider_id: str
    status: HealthStatus
    detail: str = ""
    checked_at: str = ""


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Result of a bounded cleanup attempt.

    ``quiescent=False`` is an explicit indication that the provider still
    owns live work or resources and must not be reported as cleanly closed.
    """

    provider_id: str
    quiescent: bool
    resources_released: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """Immutable embedding input owned by the caller."""

    texts: tuple[str, ...]
    model: str = ""


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Immutable embedding evidence returned by an embedding provider."""

    provider_id: str
    model: str
    embeddings: tuple[Embedding, ...]


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """Immutable description of one attended backend training operation."""

    run_id: str
    base_model: str
    base_revision: str
    dataset_digest: str


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """Immutable identity of a completed training deployment candidate."""

    provider_id: str
    run_id: str
    deployment_id: str
    model_id: str
    artifact_digest: str
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    """Immutable request to activate one already-verified release."""

    activation_id: str
    release_id: str
    version: str
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """Immutable evidence that an update activation completed."""

    provider_id: str
    activation_id: str
    release_id: str
    version: str
    artifact_digest: str
    activated_at: str = ""
    previous_version: str = ""


@runtime_checkable
class _LifecyclePort(Protocol):
    """Common lifecycle owned by each specialized provider."""

    provider_id: str

    # [any thread, thread-safe] Snapshot-only health probe.
    def health(self) -> HealthReport: ...

    # [any thread, thread-safe] Cooperative request; does not prove quiescence.
    def cancel(self, *, reason: str = "cancellation requested") -> bool: ...

    # [any thread, thread-safe] Idempotent bounded cleanup and quiescence barrier.
    def cleanup(self, timeout: float | None = None) -> CleanupResult: ...


@runtime_checkable
class EmbeddingProvider(_LifecyclePort, Protocol):
    """Port for specialized embedding computation."""

    # [any thread, async safe] Must honor context deadline and cancellation.
    def embed(
        self, request: EmbeddingRequest, context: OperationContext
    ) -> EmbeddingResult: ...


@runtime_checkable
class TrainingBackend(_LifecyclePort, Protocol):
    """Port for attended training backends that produce immutable deployment evidence."""

    # [any thread, async safe] Must honor context deadline and cancellation.
    def train(
        self, request: TrainingRequest, context: OperationContext
    ) -> DeploymentResult: ...


@runtime_checkable
class UpdateActivator(_LifecyclePort, Protocol):
    """Port for verified, atomic update activation and its immutable result."""

    # [any thread, async safe] Must honor context deadline and cancellation.
    def activate(
        self, request: ActivationRequest, context: OperationContext
    ) -> ActivationResult: ...


__all__ = [
    "ActivationRequest",
    "ActivationResult",
    "CleanupResult",
    "DeploymentResult",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResult",
    "HealthReport",
    "HealthStatus",
    "TrainingBackend",
    "TrainingRequest",
    "UpdateActivator",
]
