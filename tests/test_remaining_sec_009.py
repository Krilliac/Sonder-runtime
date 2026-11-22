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



def _artifact_files(service: RecoveryArtifactService, artifact_id: str) -> list[Path]:
    return [p for p in (service.root / f"{artifact_id}.json", service.root / f"{artifact_id}.recovery.json") if p.exists()]


def test_audit_capacity_is_checked_before_any_artifact_file_is_written(tmp_path: Path):
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user", max_audit_entries=2)
    service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    service.write("run-2", actor="same-user", kind="rollback", payload={"release": "r2"})

    with pytest.raises(RecoveryArtifactError, match="audit entry bound"):
        service.write("run-3", actor="same-user", kind="rollback", payload={"release": "r3"})

    assert _artifact_files(service, "run-3") == []
    assert len(service.audit_path.read_text(encoding="utf-8").splitlines()) == 2
    # A full log keeps verifying existing artifacts instead of bricking reads.
    assert service.inspect("run-1", actor="same-user").verified
    assert service.inspect("run-2", actor="same-user").verified


def test_payload_bound_applies_to_the_body_not_to_metadata_or_the_audit_log(tmp_path: Path):
    # The body (~85 bytes) fits; metadata and the growing audit log do not.
    service = RecoveryArtifactService((tmp_path / "recovery").resolve(), owner="same-user", max_payload_bytes=128)
    for index in range(5):
        service.write(f"run-{index}", actor="same-user", kind="rollback", payload={"r": index})
    assert service.audit_path.stat().st_size > 128
    for index in range(5):
        assert service.inspect(f"run-{index}", actor="same-user").verified


def test_failed_audit_append_rolls_back_artifact_and_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = _service(tmp_path)
    original = service._write_new

    def failing_audit(path: Path, data: bytes) -> None:
        if path.name == "audit.jsonl":
            raise RecoveryArtifactError("artifact write failed")
        original(path, data)

    monkeypatch.setattr(service, "_write_new", failing_audit)
    with pytest.raises(RecoveryArtifactError, match="write failed"):
        service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    assert _artifact_files(service, "run-1") == []

    monkeypatch.setattr(service, "_write_new", original)
    written = service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    assert service.verify("run-1", actor="same-user") == written


def test_concurrent_writes_are_serialized_and_keep_every_chained_audit_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import threading
    import time

    service = _service(tmp_path)
    original_read = service._read_audit

    def slow_read() -> list[dict[str, object]]:
        entries = original_read()
        time.sleep(0.05)  # widen the read-modify-replace window
        return entries

    monkeypatch.setattr(service, "_read_audit", slow_read)
    errors: list[Exception] = []
    start = threading.Barrier(6)

    def worker(index: int) -> None:
        start.wait()
        try:
            service.write(f"run-{index}", actor="same-user", kind="rollback", payload={"r": index})
        except (RecoveryArtifactError, OSError) as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    monkeypatch.setattr(service, "_read_audit", original_read)

    assert errors == []
    assert len(service.audit_path.read_text(encoding="utf-8").splitlines()) == 6
    for index in range(6):
        assert service.inspect(f"run-{index}", actor="same-user").verified


def test_verified_artifact_comes_from_the_digest_checked_body_not_metadata(tmp_path: Path):
    import json

    service = _service(tmp_path)
    written = service.write("run-1", actor="same-user", kind="rollback", payload={"release": "r1"})
    metadata_path = service.root / "run-1.recovery.json"
    original = json.loads(metadata_path.read_text(encoding="utf-8"))

    for field, value in (("payload", {"release": "evil"}), ("kind", "promote"), ("payload", {"release": "r1", "extra": 1})):
        tampered = dict(original)
        tampered[field] = value
        metadata_path.write_text(json.dumps(tampered), encoding="utf-8")
        inspection = service.inspect("run-1", actor="same-user")
        assert not inspection.verified
        assert inspection.artifact.payload == {"release": "r1"}
        assert inspection.artifact.kind == "rollback"
        with pytest.raises(RecoveryArtifactIntegrityError, match="verification failed"):
            service.verify("run-1", actor="same-user")

    # Metadata audit digests must agree with the chain-checked audit entry.
    tampered = dict(original)
    tampered["audit_digest"] = "f" * 64
    metadata_path.write_text(json.dumps(tampered), encoding="utf-8")
    assert not service.inspect("run-1", actor="same-user").verified

    metadata_path.write_text(json.dumps(original), encoding="utf-8")
    assert service.verify("run-1", actor="same-user") == written
