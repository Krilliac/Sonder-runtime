"""Typed application contract for durable recovery evidence.

The repository deliberately composes the existing filesystem artifact service:
the artifact path remains owner-scoped and the audit chain remains the source
of integrity truth.  The returned record is evidence, never an authorization
decision or an operating-system security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .recovery_artifacts import RecoveryArtifact, RecoveryArtifactService
from .recovery_boundary import RecoveryBoundaryAssessment, RecoveryBoundaryKind


class RecoveryEvidenceError(ValueError):
    """The typed evidence contract cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceRecord:
    """A durable artifact reference with explicit security limitations."""

    artifact: RecoveryArtifact
    path: Path
    boundary: RecoveryBoundaryAssessment
    verified: bool = True
    tamper_evident_only: bool = True

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise RecoveryEvidenceError("recovery evidence path must be absolute")
        if self.boundary.security_boundary:
            raise RecoveryEvidenceError("recovery evidence cannot claim a security boundary")
        if not self.tamper_evident_only:
            raise RecoveryEvidenceError("recovery evidence must disclose tamper-evident-only truth")

    @property
    def artifact_id(self) -> str:
        return self.artifact.artifact_id

    @property
    def owner(self) -> str:
        return self.artifact.owner


class RecoveryEvidenceRepository(Protocol):
    """Durable owner/path recovery evidence boundary."""

    def record(
        self,
        artifact_id: str,
        *,
        actor: str,
        kind: str,
        payload: Mapping[str, Any],
        resource_owner: str | None = None,
        unrestricted_selfmod: bool = False,
    ) -> RecoveryEvidenceRecord: ...

    def verify(self, artifact_id: str, *, actor: str) -> RecoveryEvidenceRecord: ...


__all__ = [
    "RecoveryEvidenceError",
    "RecoveryEvidenceRecord",
    "RecoveryEvidenceRepository",
]
