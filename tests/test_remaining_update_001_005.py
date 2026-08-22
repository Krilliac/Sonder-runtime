from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from sonder_runtime.application.updates.bounded_state import (
    BoundedUpdateState,
    MetadataChainError,
    TufLikeMetadata,
    TufLikeMetadataChain,
    UpdatePhase,
    UpdateTarget,
)
from sonder_runtime.application.updates.publication import (
    ReleaseEvidencePublication,
    SignedReleaseEvidencePublisher,
)
from sonder_runtime.application.updates.release_evidence import (
    ActivationRequest,
    AtomicReleaseActivator,
    ReleaseEvidencePackage,
    RollbackCompatibility,
    SbomComponent,
    SignedReleaseManifest,
    TestEvidence,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
FUTURE = "2026-08-21T00:00:00Z"


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _verify(_payload: bytes, signature: str, signer: str) -> bool:
    return signature == "valid" and signer in {"release-key", "tuf-key", "publisher-key"}


def _chain() -> TufLikeMetadataChain:
    entries: list[TufLikeMetadata] = []
    for role, version in (("root", 1), ("timestamp", 2), ("snapshot", 3), ("targets", 4)):
        prior = entries[-1].digest if entries else ""
        entries.append(TufLikeMetadata(role, version, FUTURE, _digest(role.encode()), "tuf-key", "valid", prior))
    return TufLikeMetadataChain(tuple(entries))


def _package(artifact_digest: str) -> ReleaseEvidencePackage:
    manifest = SignedReleaseManifest(
        "rel-2", "2.0.0", (("bundle", artifact_digest),), "release-key", "valid",
    )
    return ReleaseEvidencePackage.build(
        manifest=manifest,
        sbom=(SbomComponent("sonder-runtime", "2.0.0", "MIT", artifact_digest),),
        tests=(TestEvidence("focused", 12, report_digest=artifact_digest),),
        migrations=(),
        rollback=RollbackCompatibility(("rel-1",), True, _digest(b"restore")),
    )


def _target(artifact: bytes = b"bundle") -> tuple[UpdateTarget, bytes]:
    digest = _digest(artifact)
    package = _package(digest)
    return UpdateTarget("upd-1", "rel-2", "2.0.0", digest, package, _chain()), artifact


def test_tuf_like_chain_is_immutable_bounded_and_verifies_all_roles():
    chain = _chain()
    chain.verify(_verify, now=NOW)
    assert tuple(item.role for item in chain.entries) == ("root", "timestamp", "snapshot", "targets")
    with pytest.raises(AttributeError):
        chain.entries = ()


def test_metadata_expiry_and_link_tampering_fail_closed():
    chain = _chain()
    expired = replace(chain.entries[-1], expires_at="2026-08-19T23:59:59Z")
    with pytest.raises(MetadataChainError, match="expired"):
        TufLikeMetadataChain((*chain.entries[:-1], expired)).verify(_verify, now=NOW)
    with pytest.raises(ValueError, match="link"):
        TufLikeMetadataChain((*chain.entries[:-1], replace(chain.entries[-1], previous_digest=_digest(b"wrong"))))


class _Ports:
    def __init__(self, artifact: bytes) -> None:
        self.artifact = artifact
        self.staged: list[str] = []

    def download(self, _target: UpdateTarget) -> bytes:
        return self.artifact

    def stage(self, _target: UpdateTarget, artifact: bytes) -> str:
        self.staged.append(artifact.decode())
        return "stage-1"

    def health_check(self, _target: UpdateTarget, staged_ref: str) -> bool:
        return staged_ref == "stage-1"


class _Pointer:
    def __init__(self) -> None:
        self.value = "rel-1"

    def current(self) -> str:
        return self.value

    def commit(self, release: str) -> None:
        self.value = release


class _Helper:
    def activate(self, _request: ActivationRequest) -> None:
        return None

    def rollback(self, _request: ActivationRequest) -> None:
        return None


def test_bounded_update_lifecycle_requires_order_and_caps_history():
    target, artifact = _target()
    ports = _Ports(artifact)
    state = BoundedUpdateState(target, max_history=3)
    with pytest.raises(ValueError, match="verified"):
        state.stage(ports, artifact)
    state.download(ports)
    state.verify(_verify, now=NOW)
    state.stage(ports, artifact)
    state.health_gate(ports)
    state.activate(
        AtomicReleaseActivator(_Pointer(), _Helper()),
        ActivationRequest("linux", "rel-1", "rel-2", target.evidence.package_digest, "nonce"),
    )
    assert state.snapshot.phase is UpdatePhase.ACTIVATED
    assert len(state.snapshot.history) <= 3
    state.rollback(lambda _target: None)
    assert state.snapshot.phase is UpdatePhase.ROLLED_BACK


def test_bad_download_is_failed_and_cannot_be_staged():
    target, _artifact = _target()
    ports = _Ports(b"tampered")
    state = BoundedUpdateState(target)
    with pytest.raises(ValueError, match="digest"):
        state.download(ports)
    assert state.snapshot.phase is UpdatePhase.FAILED


def test_staging_rechecks_bytes_before_passing_them_to_the_stage_port():
    target, artifact = _target()
    ports = _Ports(artifact)
    state = BoundedUpdateState(target)
    state.download(ports)
    state.verify(_verify, now=NOW)
    with pytest.raises(ValueError, match="staged artifact"):
        state.stage(ports, b"changed")
    assert state.snapshot.phase is UpdatePhase.FAILED


def test_signed_publication_contains_hashes_sbom_tests_and_evidence():
    target, _artifact = _target()
    publication = SignedReleaseEvidencePublisher().publish(
        publication_id="pub-1",
        package=target.evidence,
        metadata=target.metadata,
        signer="publisher-key",
        sign=lambda _payload, _signer: "valid",
    )
    publication.verify(_verify, now=NOW)
    assert tuple(item.name for item in publication.targets) == (
        "SHA256SUMS", "manifest.json", "release-evidence.json", "sbom.json", "tests.json",
    )
    sums = next(item.content for item in publication.targets if item.name == "SHA256SUMS")
    assert b"manifest.json" in sums and b"sbom.json" in sums


def test_publication_target_or_signature_tampering_is_rejected():
    target, _artifact = _target()
    publication = SignedReleaseEvidencePublisher().publish(
        publication_id="pub-1", package=target.evidence, metadata=target.metadata,
        signer="publisher-key", sign=lambda _payload, _signer: "valid",
    )
    changed = replace(publication.targets[-1], content=b"changed", digest=_digest(b"changed"))
    tampered = replace(publication, targets=(*publication.targets[:-1], changed))
    with pytest.raises(ValueError, match="digest index"):
        tampered.verify(_verify, now=NOW)
    bad_manifest = replace(publication.manifest, signature="forged")
    with pytest.raises(ValueError, match="signature"):
        replace(publication, manifest=bad_manifest).verify(_verify, now=NOW)


def test_publication_reuses_release_evidence_rollback_requirements():
    target, _artifact = _target()
    package = replace(target.evidence, rollback=replace(target.evidence.rollback, tested=False))
    with pytest.raises(ValueError, match="rollback"):
        SignedReleaseEvidencePublisher().publish(
            publication_id="pub-1", package=package, metadata=target.metadata,
            signer="publisher-key", sign=lambda _payload, _signer: "valid",
        )
