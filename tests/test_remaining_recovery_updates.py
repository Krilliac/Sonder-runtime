from __future__ import annotations

from hashlib import sha256

import pytest

from sonder_runtime.application.security.recovery_boundary import (
    RecoveryBoundary,
    RecoveryBoundaryKind,
)
from sonder_runtime.application.updates.release_evidence import (
    ActivationRequest,
    AtomicReleaseActivator,
    MigrationRequirement,
    ReleaseEvidencePackage,
    RollbackCompatibility,
    SbomComponent,
    SignedReleaseManifest,
    TestEvidence,
)


def _digest(value: bytes = b"release") -> str:
    return sha256(value).hexdigest()


def test_same_user_and_unrestricted_recovery_are_never_claimed_as_security_boundary():
    assessment = RecoveryBoundary.assess(
        actor="nate", resource_owner="nate", unrestricted_selfmod=True,
        audit_files=("recovery.json",),
    )
    assert assessment.kind is RecoveryBoundaryKind.SAME_USER
    assert not assessment.security_boundary
    assert not RecoveryBoundary.can_claim_security_boundary(assessment)
    assert "unrestricted" in RecoveryBoundary.recovery_notice(assessment)


def test_external_owner_is_explicitly_limited_too():
    assessment = RecoveryBoundary.assess(actor="agent", resource_owner="owner")
    assert assessment.kind is RecoveryBoundaryKind.EXTERNAL
    assert "authorization boundary" in " ".join(assessment.limitations)


def _package() -> ReleaseEvidencePackage:
    manifest = SignedReleaseManifest(
        "rel-1", "1.0.0", (("bundle", _digest()),), "release-key", "sig",
        (("python", "3.12"),),
    )
    return ReleaseEvidencePackage.build(
        manifest=manifest,
        sbom=(SbomComponent("sonder-runtime", "1.0.0", "MIT", _digest()),),
        tests=(TestEvidence("focused", 10, report_digest=_digest(b"tests")),),
        migrations=(MigrationRequirement("updates", 2, True),),
        rollback=RollbackCompatibility(("rel-0",), True, _digest(b"restore")),
    )


def test_release_evidence_requires_signature_and_rollback_proof():
    package = _package()
    package.verify(lambda payload, signature, signer: payload and signature == "sig" and signer == "release-key")
    with pytest.raises(ValueError, match="signature"):
        package.verify(lambda *_: False)


class _Pointer:
    def __init__(self) -> None:
        self.value = "rel-0"

    def current(self) -> str:
        return self.value

    def commit(self, target_release: str) -> None:
        self.value = target_release


class _Helper:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def activate(self, request: ActivationRequest) -> None:
        self.calls.append("activate")
        if self.fail:
            raise RuntimeError("helper failed")

    def rollback(self, request: ActivationRequest) -> None:
        self.calls.append("rollback")


@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
def test_platform_helper_contract_and_atomic_rollback(platform: str):
    pointer, helper = _Pointer(), _Helper(fail=True)
    request = ActivationRequest(platform, "rel-0", "rel-1", _digest(b"evidence"), "nonce")
    with pytest.raises(RuntimeError, match="helper failed"):
        AtomicReleaseActivator(pointer, helper).activate(request)
    assert pointer.value == "rel-0"
    assert helper.calls == ["activate", "rollback"]

