"""Signed release evidence and platform-neutral atomic activation contracts.

The module is deliberately an application contract.  It validates immutable
evidence and coordinates an injected helper process/pointer store; it does
not execute platform commands or claim that a failed runtime can repair
itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Callable, Mapping, Protocol


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")


@dataclass(frozen=True, slots=True)
class SignedReleaseManifest:
    release_id: str
    version: str
    artifact_hashes: tuple[tuple[str, str], ...]
    signer: str
    signature: str
    runtime_contract: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for value, field in ((self.release_id, "release_id"), (self.version, "version"), (self.signer, "signer"), (self.signature, "signature")):
            _require_text(value, field)
        if not self.artifact_hashes:
            raise ValueError("at least one artifact hash is required")
        names = [name for name, _ in self.artifact_hashes]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("artifact names must be unique and non-empty")
        for name, digest in self.artifact_hashes:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
                raise ValueError(f"artifact {name!r} must have a SHA-256 hash")

    def signing_bytes(self) -> bytes:
        return _canonical({
            "release_id": self.release_id,
            "version": self.version,
            "artifact_hashes": self.artifact_hashes,
            "runtime_contract": self.runtime_contract,
            "signer": self.signer,
        })

    @property
    def digest(self) -> str:
        return sha256(self.signing_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class SbomComponent:
    name: str
    version: str
    license: str = "unknown"
    digest: str = ""

    def __post_init__(self) -> None:
        _require_text(self.name, "component name")
        _require_text(self.version, "component version")
        if self.digest and (len(self.digest) != 64 or any(c not in "0123456789abcdef" for c in self.digest.lower())):
            raise ValueError("component digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class TestEvidence:
    # Prevent pytest from treating this domain value object as a test class
    # when it is imported into a test module.
    __test__ = False
    suite: str
    passed: int
    failed: int = 0
    skipped: int = 0
    report_digest: str = ""

    def __post_init__(self) -> None:
        _require_text(self.suite, "test suite")
        if any(type(value) is not int or value < 0 for value in (self.passed, self.failed, self.skipped)):
            raise ValueError("test counts must be non-negative integers")
        if self.report_digest and len(self.report_digest) != 64:
            raise ValueError("report_digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class MigrationRequirement:
    store: str
    minimum_schema: int
    reversible: bool

    def __post_init__(self) -> None:
        _require_text(self.store, "migration store")
        if type(self.minimum_schema) is not int or self.minimum_schema < 0:
            raise ValueError("minimum_schema must be non-negative")


@dataclass(frozen=True, slots=True)
class RollbackCompatibility:
    prior_release_ids: tuple[str, ...]
    tested: bool
    restore_proof_digest: str

    def __post_init__(self) -> None:
        if not self.prior_release_ids or any(not item for item in self.prior_release_ids):
            raise ValueError("at least one prior release is required")
        _require_text(self.restore_proof_digest, "restore_proof_digest")


@dataclass(frozen=True, slots=True)
class ReleaseEvidencePackage:
    manifest: SignedReleaseManifest
    sbom: tuple[SbomComponent, ...]
    tests: tuple[TestEvidence, ...]
    migrations: tuple[MigrationRequirement, ...]
    rollback: RollbackCompatibility
    package_digest: str

    @classmethod
    def build(cls, *, manifest: SignedReleaseManifest, sbom: tuple[SbomComponent, ...], tests: tuple[TestEvidence, ...], migrations: tuple[MigrationRequirement, ...], rollback: RollbackCompatibility) -> "ReleaseEvidencePackage":
        if not sbom or not tests:
            raise ValueError("release evidence requires an SBOM and test evidence")
        material = {
            "manifest": manifest.digest,
            "sbom": [(item.name, item.version, item.license, item.digest) for item in sbom],
            "tests": [(item.suite, item.passed, item.failed, item.skipped, item.report_digest) for item in tests],
            "migrations": [(item.store, item.minimum_schema, item.reversible) for item in migrations],
            "rollback": (rollback.prior_release_ids, rollback.tested, rollback.restore_proof_digest),
        }
        return cls(manifest, tuple(sbom), tuple(tests), tuple(migrations), rollback, _digest(material))

    def _material(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.digest,
            "sbom": [(item.name, item.version, item.license, item.digest) for item in self.sbom],
            "tests": [(item.suite, item.passed, item.failed, item.skipped, item.report_digest) for item in self.tests],
            "migrations": [(item.store, item.minimum_schema, item.reversible) for item in self.migrations],
            "rollback": (self.rollback.prior_release_ids, self.rollback.tested, self.rollback.restore_proof_digest),
        }

    def verify(
        self,
        verifier: Callable[[bytes, str, str], bool],
        *,
        expected_runtime_contract: Mapping[str, str] | None = None,
    ) -> None:
        if _digest(self._material()) != self.package_digest:
            raise ValueError("release evidence package digest mismatch")
        if not verifier(self.manifest.signing_bytes(), self.manifest.signature, self.manifest.signer):
            raise ValueError("release manifest signature rejected")
        if expected_runtime_contract is not None:
            actual = dict(self.manifest.runtime_contract)
            if actual != {str(key): str(value) for key, value in expected_runtime_contract.items()}:
                raise ValueError("sealed runtime contract mismatch")
        if any(item.failed for item in self.tests):
            raise ValueError("release contains failed test evidence")
        if not self.rollback.tested:
            raise ValueError("rollback compatibility is not tested")


class PlatformActivationHelper(Protocol):
    """Out-of-process activation contract for Linux, Windows, or macOS."""

    def activate(self, request: "ActivationRequest") -> None: ...
    def rollback(self, request: "ActivationRequest") -> None: ...


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    platform: str
    current_release: str
    target_release: str
    release_evidence_digest: str
    helper_nonce: str

    def __post_init__(self) -> None:
        if self.platform not in {"linux", "windows", "macos"}:
            raise ValueError("unsupported activation platform")
        for value, field in ((self.current_release, "current_release"), (self.target_release, "target_release"), (self.release_evidence_digest, "release_evidence_digest"), (self.helper_nonce, "helper_nonce")):
            _require_text(value, field)


class ReleasePointer(Protocol):
    def current(self) -> str: ...
    def commit(self, target_release: str) -> None: ...


class AtomicReleaseActivator:
    """Coordinate helper activation and rollback from the known-good route."""

    def __init__(self, pointer: ReleasePointer, helper: PlatformActivationHelper) -> None:
        self._pointer = pointer
        self._helper = helper

    def activate(self, request: ActivationRequest) -> str:
        current = self._pointer.current()
        if current != request.current_release:
            raise RuntimeError("current release changed before activation")
        try:
            self._helper.activate(request)
            self._pointer.commit(request.target_release)
        except Exception:
            # Rollback is delegated to the independent helper and is attempted
            # before surfacing the activation failure.  The failed runtime is
            # never used as the recovery mechanism.
            rollback_request = ActivationRequest(
                request.platform,
                request.target_release,
                request.current_release,
                request.release_evidence_digest,
                request.helper_nonce,
            )
            self._helper.rollback(rollback_request)
            self._pointer.commit(current)
            raise
        return request.target_release


__all__ = [
    "ActivationRequest", "AtomicReleaseActivator", "MigrationRequirement",
    "PlatformActivationHelper", "ReleaseEvidencePackage", "ReleasePointer",
    "RollbackCompatibility", "SbomComponent", "SignedReleaseManifest",
    "TestEvidence",
]
