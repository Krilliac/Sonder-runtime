from pathlib import Path

import pytest

from sonder_runtime.adapters.security.recovery_evidence import (
    FilesystemRecoveryEvidenceRepository,
)
from sonder_runtime.application.security.recovery_artifacts import (
    RecoveryArtifactIntegrityError,
    RecoveryArtifactOwnershipError,
    RecoveryArtifactService,
)
from sonder_runtime.application.security.recovery_boundary import RecoveryBoundaryKind


def _repository(tmp_path: Path) -> FilesystemRecoveryEvidenceRepository:
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user")
    return FilesystemRecoveryEvidenceRepository(service)


def test_repository_returns_typed_durable_owner_path_evidence(tmp_path: Path):
    repository = _repository(tmp_path)
    first = repository.record(
        "run-1", actor="same-user", kind="rollback", payload={"release": "r1"}
    )
    second = repository.record(
        "run-2", actor="same-user", kind="rollback", payload={"release": "r2"}
    )

    assert first.path == (tmp_path / "recovery" / "run-1.json").resolve()
    assert first.verified and first.tamper_evident_only
    assert first.boundary.kind is RecoveryBoundaryKind.SAME_USER
    assert "not a security boundary" in first.boundary.limitations[0]
    assert second.artifact.previous_audit_digest == first.artifact.audit_digest
    assert repository.verify("run-2", actor="same-user").artifact == second.artifact


def test_repository_preserves_fail_closed_integrity_and_owner_boundary(tmp_path: Path):
    repository = _repository(tmp_path)
    repository.record("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    artifact = tmp_path / "recovery" / "run-1.json"
    artifact.write_text(artifact.read_text(encoding="utf-8").replace('"r1"', '"evil"'), encoding="utf-8")

    with pytest.raises(RecoveryArtifactIntegrityError, match="verification failed"):
        repository.verify("run-1", actor="same-user")
    with pytest.raises(RecoveryArtifactOwnershipError):
        repository.verify("run-1", actor="different-user")


def test_unrestricted_same_user_evidence_is_explicitly_not_security(tmp_path: Path):
    repository = _repository(tmp_path)
    evidence = repository.record(
        "run-1",
        actor="same-user",
        kind="rollback",
        payload={},
        unrestricted_selfmod=True,
    )

    assert evidence.boundary.kind is RecoveryBoundaryKind.SAME_USER
    assert evidence.boundary.security_boundary is False
    notice = " ".join(evidence.boundary.limitations)
    assert "alter recovery state and audit files" in notice
