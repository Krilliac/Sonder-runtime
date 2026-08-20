from pathlib import Path

import pytest

from sonder_runtime.application.security.recovery_artifacts import (
    RecoveryArtifactError,
    RecoveryArtifactIntegrityError,
    RecoveryArtifactOwnershipError,
    RecoveryArtifactService,
)


def _service(tmp_path: Path) -> RecoveryArtifactService:
    return RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user")


def test_write_inspect_and_verify_artifact_with_chained_audit(tmp_path: Path):
    service = _service(tmp_path)
    first = service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    second = service.write("run-2", actor="same-user", kind="rollback", payload={"release": "r2"})

    assert service.verify("run-1", actor="same-user") == first
    inspection = service.inspect("run-2", actor="same-user")
    assert inspection.verified
    assert inspection.tamper_evident_only
    assert second.previous_audit_digest == first.audit_digest


def test_actor_and_artifact_ids_are_guarded(tmp_path: Path):
    service = _service(tmp_path)
    with pytest.raises(RecoveryArtifactOwnershipError):
        service.write("run-1", actor="different-user", kind="rollback", payload={})
    with pytest.raises(RecoveryArtifactOwnershipError):
        service.inspect("run-1", actor="different-user")
    with pytest.raises(RecoveryArtifactError, match="invalid"):
        service.write("../escape", actor="same-user", kind="rollback", payload={})


def test_payload_and_audit_tampering_are_detected(tmp_path: Path):
    service = _service(tmp_path)
    service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    artifact_path = service.root / "run-1.json"
    artifact_path.write_text('{"artifact_id":"run-1","kind":"rollback","owner":"same-user","payload":{"release":"evil"}}', encoding="utf-8")
    with pytest.raises(RecoveryArtifactIntegrityError, match="verification failed"):
        service.verify("run-1", actor="same-user")

    # Replacing the audit line is also detected by the chained digest.
    artifact_path.write_text('{"artifact_id":"run-1","kind":"rollback","owner":"same-user","payload":{"release":"r1"}}', encoding="utf-8")
    service.audit_path.write_text('{"artifact_digest":"' + "0" * 64 + '","artifact_id":"run-1","audit_digest":"' + "0" * 64 + '","owner":"same-user","previous":""}\n', encoding="utf-8")
    with pytest.raises(RecoveryArtifactIntegrityError, match="verification failed"):
        service.verify("run-1", actor="same-user")


def test_same_user_limit_is_explicit_when_metadata_and_audit_are_replaced(tmp_path: Path):
    service = _service(tmp_path)
    service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    assert service.inspect("run-1", actor="same-user").tamper_evident_only is True
    assert "same-user" in service.__class__.__doc__


def test_root_must_be_absolute_and_payload_is_bounded(tmp_path: Path):
    with pytest.raises(RecoveryArtifactError, match="absolute"):
        RecoveryArtifactService("relative-root", owner="same-user")
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user", max_payload_bytes=20)
    with pytest.raises(RecoveryArtifactError, match="exceeds bound"):
        service.write("run-1", actor="same-user", kind="rollback", payload={"large": "x" * 100})

