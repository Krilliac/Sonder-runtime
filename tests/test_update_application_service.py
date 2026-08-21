from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest

from sonder_runtime.application.updates import (
    TufLikeMetadata, TufLikeMetadataChain, UpdateApplicationService,
    UpdateAuthorizationError, UpdateTarget,
)
from sonder_runtime.application.updates.durable_activation import DurableActivationCoordinator
from sonder_runtime.application.updates.release_evidence import (
    ActivationRequest, ReleaseEvidencePackage, RollbackCompatibility,
    SbomComponent, SignedReleaseManifest, TestEvidence,
)


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def verifier(_payload: bytes, signature: str, signer: str) -> bool:
    return signature == "valid" and signer in {"release-key", "tuf-key"}


def target() -> tuple[UpdateTarget, bytes]:
    artifact = b"release"
    manifest = SignedReleaseManifest(
        "rel-2", "2.0.0", (("bundle", digest(artifact)),), "release-key", "valid",
        (("python", "3.12"),),
    )
    package = ReleaseEvidencePackage.build(
        manifest=manifest, sbom=(SbomComponent("runtime", "2.0.0"),),
        tests=(TestEvidence("focused", 1),), migrations=(),
        rollback=RollbackCompatibility(("rel-1",), True, "restore-proof"),
    )
    entries = []
    for role, version in (("root", 1), ("timestamp", 2), ("snapshot", 3), ("targets", 4)):
        prior = entries[-1].digest if entries else ""
        entries.append(TufLikeMetadata(role, version, "2026-08-22T00:00:00Z",
                                       digest(role.encode()), "tuf-key", "valid", prior))
    return UpdateTarget("u1", "rel-2", "2.0.0", digest(artifact), package,
                        TufLikeMetadataChain(tuple(entries))), artifact


class Ports:
    def __init__(self, artifact: bytes, health: bool = True):
        self.artifact, self.health = artifact, health
    def download(self, _target): return self.artifact
    def stage(self, _target, _artifact): return "stage-1"
    def health_check(self, _target, _staged): return self.health


class Backup:
    def __init__(self): self.created, self.restored = [], []
    def create(self, _target): self.created.append("b1"); return "b1"
    def verify(self, backup_id): return backup_id == "b1"
    def restore(self, backup_id): self.restored.append(backup_id)


class Pointer:
    def __init__(self): self.value = "rel-1"
    def current(self): return self.value
    def commit(self, value): self.value = value


class Helper:
    def __init__(self, fail=False): self.fail, self.calls = fail, []
    def activate(self, _request):
        self.calls.append("activate")
        if self.fail: raise RuntimeError("helper failed")
    def rollback(self, _request): self.calls.append("rollback")


class Journal:
    def __init__(self): self.rows = []
    def append(self, entry): self.rows.append(entry)
    def entries(self): return tuple(self.rows)


class Authority:
    def __init__(self, allowed=True): self.allowed = allowed
    def authorize(self, _target): return self.allowed


def service(ports, backup, authority, helper=None):
    pointer, helper = Pointer(), helper or Helper()
    coordinator = DurableActivationCoordinator(pointer, helper, Journal(), verifier)
    return (UpdateApplicationService(ports=ports, backup=backup, activation=coordinator,
                                      verifier=verifier, authority=authority), pointer, helper)


def request(update_target):
    return ActivationRequest("linux", "rel-1", "rel-2",
                             update_target.evidence.package_digest, "nonce")


def test_application_service_composes_signed_prepare_runtime_and_atomic_activation():
    update_target, artifact = target()
    backup = Backup()
    app, pointer, _ = service(Ports(artifact), backup, Authority())
    prepared = app.prepare(update_target, now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    result = app.activate(prepared, activation_id="a1", request=request(update_target),
                          observed_dependencies={"python": "3.12"})
    assert result.phase.value == "activated"
    assert pointer.value == "rel-2"
    assert backup.restored == []


def test_authority_denial_is_fail_closed_and_activation_is_not_called():
    update_target, artifact = target()
    backup = Backup()
    app, pointer, helper = service(Ports(artifact), backup, Authority(False))
    with pytest.raises(UpdateAuthorizationError):
        app.prepare(update_target, now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert pointer.value == "rel-1"
    assert helper.calls == []
    assert backup.created == []


def test_activation_failure_restores_backup_after_durable_recovery():
    update_target, artifact = target()
    backup = Backup()
    app, pointer, helper = service(Ports(artifact), backup, Authority(), Helper(True))
    prepared = app.prepare(update_target, now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    with pytest.raises(RuntimeError, match="helper failed"):
        app.activate(prepared, activation_id="a1", request=request(update_target),
                     observed_dependencies={"python": "3.12"})
    assert pointer.value == "rel-1"
    assert helper.calls == ["activate", "rollback"]
    assert backup.restored == ["b1"]
