"""Deterministic signed publication of release evidence.

Publication is an in-memory bundle.  A caller may persist or transport its
bytes through an adapter, but this contract performs no I/O and never signs
or publishes by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Callable

from .bounded_state import TufLikeMetadataChain
from .release_evidence import ReleaseEvidencePackage


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    name: str
    content: bytes
    digest: str

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("publication target name must be a flat non-empty name")
        if not isinstance(self.content, bytes) or self.digest != _digest(self.content):
            raise ValueError("publication target digest mismatch")


@dataclass(frozen=True, slots=True)
class SignedPublicationManifest:
    publication_id: str
    package_digest: str
    metadata_chain_digest: str
    target_digests: tuple[tuple[str, str], ...]
    signer: str
    signature: str

    def signing_bytes(self) -> bytes:
        return _canonical({
            "publication_id": self.publication_id,
            "package_digest": self.package_digest,
            "metadata_chain_digest": self.metadata_chain_digest,
            "target_digests": self.target_digests,
            "signer": self.signer,
        })

    @property
    def digest(self) -> str:
        return _digest(self.signing_bytes())


@dataclass(frozen=True, slots=True)
class ReleaseEvidencePublication:
    package: ReleaseEvidencePackage
    metadata: TufLikeMetadataChain
    manifest: SignedPublicationManifest
    targets: tuple[PublicationTarget, ...]

    @property
    def digest(self) -> str:
        return _digest(self.manifest.signing_bytes() + b"".join(item.content for item in self.targets))

    def verify(
        self,
        verifier: Callable[[bytes, str, str], bool],
        *,
        now=None,
        expected_target_names: tuple[str, ...] = ("manifest.json", "sbom.json", "tests.json", "release-evidence.json", "SHA256SUMS"),
    ) -> None:
        self.metadata.verify(verifier, now=now)
        self.package.verify(verifier)
        if self.manifest.package_digest != self.package.package_digest:
            raise ValueError("publication package digest mismatch")
        if self.manifest.metadata_chain_digest != self.metadata.digest:
            raise ValueError("publication metadata chain digest mismatch")
        names = tuple(item.name for item in self.targets)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("publication targets must be unique and sorted")
        if tuple(sorted(expected_target_names)) != names:
            raise ValueError("publication target set is incomplete")
        actual = tuple((item.name, item.digest) for item in self.targets)
        if actual != self.manifest.target_digests:
            raise ValueError("publication target digest index mismatch")
        if not verifier(self.manifest.signing_bytes(), self.manifest.signature, self.manifest.signer):
            raise ValueError("publication signature rejected")


class SignedReleaseEvidencePublisher:
    """Build a deterministic publication from existing release evidence."""

    def publish(
        self,
        *,
        publication_id: str,
        package: ReleaseEvidencePackage,
        metadata: TufLikeMetadataChain,
        signer: str,
        sign: Callable[[bytes, str], str],
    ) -> ReleaseEvidencePublication:
        if not publication_id or not signer:
            raise ValueError("publication_id and signer are required")
        if not package.rollback.tested:
            raise ValueError("release evidence rollback compatibility is not tested")
        if any(item.failed for item in package.tests):
            raise ValueError("release evidence contains failed test evidence")
        evidence = {
            "manifest_digest": package.manifest.digest,
            "package_digest": package.package_digest,
            "migrations": [(item.store, item.minimum_schema, item.reversible) for item in package.migrations],
            "rollback": (package.rollback.prior_release_ids, package.rollback.tested, package.rollback.restore_proof_digest),
        }
        sbom = [{"name": item.name, "version": item.version, "license": item.license, "digest": item.digest} for item in package.sbom]
        tests = [{"suite": item.suite, "passed": item.passed, "failed": item.failed, "skipped": item.skipped, "report_digest": item.report_digest} for item in package.tests]
        payloads = {
            "manifest.json": package.manifest.signing_bytes(),
            "sbom.json": _canonical(sbom),
            "tests.json": _canonical(tests),
            "release-evidence.json": _canonical(evidence),
        }
        payloads["SHA256SUMS"] = "\n".join(
            f"{_digest(payloads[name])}  {name}" for name in sorted(payloads)
        ).encode("ascii") + b"\n"
        targets = tuple(PublicationTarget(name, payloads[name], _digest(payloads[name])) for name in sorted(payloads))
        target_digests = tuple((item.name, item.digest) for item in targets)
        unsigned = SignedPublicationManifest(publication_id, package.package_digest, metadata.digest, target_digests, signer, "pending")
        signature = sign(unsigned.signing_bytes(), signer)
        manifest = SignedPublicationManifest(publication_id, package.package_digest, metadata.digest, target_digests, signer, signature)
        return ReleaseEvidencePublication(package, metadata, manifest, targets)


__all__ = [
    "PublicationTarget", "ReleaseEvidencePublication",
    "SignedPublicationManifest", "SignedReleaseEvidencePublisher",
]
