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


def _sealed_mapping(value: Mapping[str, str], field: str) -> tuple[tuple[str, str], ...]:
    """Normalize a dependency contract without allowing ambiguous entries."""
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{field} must be a non-empty mapping")
    rows: list[tuple[str, str]] = []
    for name, version in value.items():
        _require_text(name, f"{field} name")
        _require_text(version, f"{field} value")
        rows.append((str(name), str(version)))
    return tuple(sorted(rows))


@dataclass(frozen=True, slots=True)
class SealedRuntimeContract:
    """Canonical, immutable dependency contract for a release."""

    dependencies: tuple[tuple[str, str], ...]
    digest: str

    @classmethod
    def seal(cls, dependencies: Mapping[str, str]) -> "SealedRuntimeContract":
        rows = _sealed_mapping(dependencies, "runtime dependencies")
        return cls(rows, _digest(rows))

    @classmethod
    def from_manifest(cls, dependencies: tuple[tuple[str, str], ...]) -> "SealedRuntimeContract":
        rows = tuple(sorted((str(name), str(version)) for name, version in dependencies))
        if not rows or any(not name or not version for name, version in rows):
            raise ValueError("manifest runtime contract must be non-empty")
        if len({name for name, _ in rows}) != len(rows):
            raise ValueError("manifest runtime contract contains duplicate dependencies")
        return cls(rows, _digest(rows))

    def verify_exact(self, observed: Mapping[str, str]) -> None:
        actual = _sealed_mapping(observed, "observed runtime dependencies")
        if actual != self.dependencies:
            raise ValueError("exact sealed runtime dependency contract mismatch")
        if _digest(self.dependencies) != self.digest:
            raise ValueError("sealed runtime dependency contract digest mismatch")


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
            SealedRuntimeContract.from_manifest(self.manifest.runtime_contract).verify_exact(expected_runtime_contract)
        if any(item.failed for item in self.tests):
            raise ValueError("release contains failed test evidence")
        if not self.rollback.tested:
            raise ValueError("rollback compatibility is not tested")


class PlatformActivationHelper(Protocol):
    """Out-of-process activation contract for Linux, Windows, or macOS."""

    def activate(self, request: "ActivationRequest") -> None: ...
    def rollback(self, request: "ActivationRequest") -> None: ...


class RecoveryEvidenceSink(Protocol):
    """Independent recorder for activation recovery evidence."""

    def record(self, evidence: "StandaloneRecoveryEvidence") -> None: ...


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    platform: str
    current_release: str
    target_release: str
    release_evidence_digest: str
    helper_nonce: str
    helper_argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.platform not in {"linux", "windows", "macos"}:
            raise ValueError("unsupported activation platform")
        for value, field in ((self.current_release, "current_release"), (self.target_release, "target_release"), (self.release_evidence_digest, "release_evidence_digest"), (self.helper_nonce, "helper_nonce")):
            _require_text(value, field)
        if self.helper_argv and any(not isinstance(item, str) or not item for item in self.helper_argv):
            raise ValueError("helper_argv must contain non-empty strings")


@dataclass(frozen=True, slots=True)
class StandaloneRecoveryEvidence:
    """Proof that recovery was attempted outside the failed release."""

    platform: str
    previous_release: str
    failed_release: str
    helper_rollback_attempted: bool
    pointer_restore_attempted: bool
    pointer_restored: bool
    error_types: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return _digest({
            "platform": self.platform,
            "previous_release": self.previous_release,
            "failed_release": self.failed_release,
            "helper_rollback_attempted": self.helper_rollback_attempted,
            "pointer_restore_attempted": self.pointer_restore_attempted,
            "pointer_restored": self.pointer_restored,
            "error_types": self.error_types,
        })


class ActivationRecoveryError(RuntimeError):
    """Activation failed and its independent recovery proof is incomplete."""

    def __init__(self, evidence: StandaloneRecoveryEvidence) -> None:
        super().__init__("activation failed and independent recovery was incomplete")
        self.evidence = evidence


class ReleasePointer(Protocol):
    def current(self) -> str: ...
    def commit(self, target_release: str) -> None: ...


class AtomicReleaseActivator:
    """Coordinate helper activation and rollback from the known-good route."""

    def __init__(self, pointer: ReleasePointer, helper: PlatformActivationHelper, evidence_sink: RecoveryEvidenceSink | None = None) -> None:
        self._pointer = pointer
        self._helper = helper
        self._evidence_sink = evidence_sink
        self._recovery: list[StandaloneRecoveryEvidence] = []

    @property
    def recovery_evidence(self) -> tuple[StandaloneRecoveryEvidence, ...]:
        return tuple(self._recovery)

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
                request.helper_argv,
            )
            errors: list[str] = []
            try:
                self._helper.rollback(rollback_request)
            except Exception as recovery_error:
                errors.append(type(recovery_error).__name__)
            pointer_restored = False
            try:
                self._pointer.commit(current)
                pointer_restored = self._pointer.current() == current
            except Exception as recovery_error:
                errors.append(type(recovery_error).__name__)
            evidence = StandaloneRecoveryEvidence(
                request.platform, current, request.target_release, True, True,
                pointer_restored, tuple(errors),
            )
            self._recovery.append(evidence)
            if self._evidence_sink is not None:
                self._evidence_sink.record(evidence)
            if not pointer_restored:
                raise ActivationRecoveryError(evidence) from None
            raise
        return request.target_release


__all__ = [
    "ActivationRequest", "AtomicReleaseActivator", "MigrationRequirement",
    "ActivationRecoveryError", "RecoveryEvidenceSink", "SealedRuntimeContract",
    "StandaloneRecoveryEvidence",
    "PlatformActivationHelper", "ReleaseEvidencePackage", "ReleasePointer",
    "RollbackCompatibility", "SbomComponent", "SignedReleaseManifest",
    "TestEvidence",
]
