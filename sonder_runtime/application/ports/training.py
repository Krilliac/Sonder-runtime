"""Typed side-effect ports for attended adaptive training.

The application layer owns sequencing and attendance policy.  Adapters own
processes, filesystem locks, durable journals, and Ollama mutations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ContextManager, Protocol


@dataclass(frozen=True, slots=True)
class TrainingLaunchRequest:
    run_id: str
    command: tuple[str, ...]
    manifest_digest: str


@dataclass(frozen=True, slots=True)
class TrainingLaunchResult:
    run_id: str
    exit_code: int
    adapter_digest: str
    detail: str = ""


class TrainingProcessPort(Protocol):
    """Launch one already-authorized training process."""

    # [any thread, async safe] Adapter must not widen the supplied command.
    def launch(self, request: TrainingLaunchRequest) -> TrainingLaunchResult: ...


class TrainingLockPort(Protocol):
    """Exclusive lifecycle lock; acquisition failure must fail closed."""

    # [any thread, thread-safe] Context owns acquisition and release.
    def acquire(self, run_id: str) -> ContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class ManifestEvidence:
    manifest_digest: str
    signature: str


class ManifestVerifierPort(Protocol):
    """Verify signed evidence for an immutable manifest digest."""

    # [any thread, thread-safe] False is a hard authorization failure.
    def verify(self, evidence: ManifestEvidence) -> bool: ...


@dataclass(frozen=True, slots=True)
class JournalEvent:
    run_id: str
    phase: str
    manifest_digest: str
    detail: str = ""


class TrainingJournalPort(Protocol):
    """Durable append/recovery evidence for one training transition."""

    # [any thread, thread-safe] Must be durable before returning.
    def append(self, event: JournalEvent) -> None: ...


class OllamaPolicyPort(Protocol):
    """Sole port allowed to mutate the Ollama-backed runtime policy."""

    # [any thread, thread-safe] Returns an opaque prior state for restoration.
    def reserve(self, run_id: str, artifact_digest: str) -> object: ...
    def commit(self, reservation: object) -> None: ...
    def restore(self, reservation: object) -> None: ...


class TrainingDeploymentPort(Protocol):
    """Existing attended health-gated deployment/rollback contract."""

    # [any thread, thread-safe] Implementations must health-gate activation.
    def activate(self, artifact_id: str, *, attended: bool = False): ...
    def rollback(self, *, attended: bool = False, reason: str = "operator rollback"): ...


__all__ = [
    "JournalEvent",
    "ManifestEvidence",
    "ManifestVerifierPort",
    "OllamaPolicyPort",
    "TrainingDeploymentPort",
    "TrainingJournalPort",
    "TrainingLaunchRequest",
    "TrainingLaunchResult",
    "TrainingLockPort",
    "TrainingProcessPort",
]
