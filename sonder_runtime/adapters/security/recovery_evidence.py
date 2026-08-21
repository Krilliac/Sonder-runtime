"""Filesystem adapter for the typed durable recovery evidence contract."""

from __future__ import annotations

from typing import Any, Mapping

from ...application.security.recovery_artifacts import RecoveryArtifactService
from ...application.security.recovery_boundary import RecoveryBoundary
from ...application.security.recovery_evidence import (
    RecoveryEvidenceRecord,
    RecoveryEvidenceRepository,
)


class FilesystemRecoveryEvidenceRepository:
    """Compose owner/path artifacts and chained audit integrity into evidence.

    The underlying artifact service is the durable store.  This adapter adds
    a typed read/write boundary and carries the mandatory same-user disclosure
    alongside every verified record.
    """

    def __init__(self, artifacts: RecoveryArtifactService) -> None:
        self._artifacts = artifacts

    def record(
        self,
        artifact_id: str,
        *,
        actor: str,
        kind: str,
        payload: Mapping[str, Any],
        resource_owner: str | None = None,
        unrestricted_selfmod: bool = False,
    ) -> RecoveryEvidenceRecord:
        boundary = RecoveryBoundary.assess(
            actor=actor,
            resource_owner=resource_owner or self._artifacts.owner,
            unrestricted_selfmod=unrestricted_selfmod,
            audit_files=(str(self._artifacts.audit_path),),
        )
        artifact = self._artifacts.write(
            artifact_id,
            actor=actor,
            kind=kind,
            payload=payload,
        )
        return RecoveryEvidenceRecord(
            artifact=artifact,
            path=(self._artifacts.root / f"{artifact.artifact_id}.json").resolve(),
            boundary=boundary,
        )

    def verify(self, artifact_id: str, *, actor: str) -> RecoveryEvidenceRecord:
        inspection = self._artifacts.inspect(artifact_id, actor=actor)
        if not inspection.verified:
            # Preserve the artifact service's fail-closed exception and exact
            # same-user warning by using its verification boundary.
            artifact = self._artifacts.verify(artifact_id, actor=actor)
        else:
            artifact = inspection.artifact
        boundary = RecoveryBoundary.assess(
            actor=actor,
            resource_owner=artifact.owner,
            audit_files=(str(self._artifacts.audit_path),),
        )
        return RecoveryEvidenceRecord(
            artifact=artifact,
            path=inspection.path.resolve(),
            boundary=boundary,
            verified=inspection.verified,
        )


__all__ = ["FilesystemRecoveryEvidenceRepository"]
