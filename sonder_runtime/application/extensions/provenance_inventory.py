"""Deterministic extension provenance, SBOM, and admission-health records.

The service is deliberately storage- and execution-neutral.  It records the
facts needed to audit an extension installation, derives a stable SBOM, and
turns the existing compatibility/quarantine decision into an explicit health
state.  Signature verification is supplied by the composition root; this
module never imports extension code or performs network/filesystem I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from typing import Callable, Iterable, Mapping

from sonder_runtime.application.extensions.quarantine import (
    QuarantineDecision,
    QuarantineRegistry,
)
from sonder_runtime.domain.extensions.manifest import ExtensionManifest


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")


def _sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"{field} must be a SHA-256 digest")


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    REVIEWED = "reviewed"
    TRUSTED = "trusted"


class ExtensionHealthState(StrEnum):
    HEALTHY = "healthy"
    INCOMPATIBLE = "incompatible"
    QUARANTINED = "quarantined"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class SignatureRecord:
    signer: str
    algorithm: str
    signature: str
    signed_digest: str
    verified: bool = False

    def __post_init__(self) -> None:
        for value, field in ((self.signer, "signer"), (self.algorithm, "algorithm"), (self.signature, "signature")):
            _text(value, field)
        _sha256(self.signed_digest, "signed_digest")


@dataclass(frozen=True, slots=True)
class TrustRecord:
    source: str
    level: TrustLevel
    basis: str
    issuer: str = ""

    def __post_init__(self) -> None:
        for value, field in ((self.source, "source"), (self.basis, "basis")):
            _text(value, field)
        if self.issuer:
            _text(self.issuer, "issuer")


@dataclass(frozen=True, slots=True)
class ExtensionProvenance:
    extension_id: str
    version: str
    source: str
    artifact_digest: str
    manifest_digest: str
    signature: SignatureRecord
    trust: TrustRecord

    def __post_init__(self) -> None:
        for value, field in ((self.extension_id, "extension_id"), (self.version, "version"), (self.source, "source")):
            _text(value, field)
        _sha256(self.artifact_digest, "artifact_digest")
        _sha256(self.manifest_digest, "manifest_digest")
        if self.signature.signed_digest != self.manifest_digest:
            raise ValueError("signature must bind the extension manifest digest")

    def signing_material(self) -> bytes:
        return _canonical({
            "extension_id": self.extension_id,
            "version": self.version,
            "source": self.source,
            "artifact_digest": self.artifact_digest,
            "manifest_digest": self.manifest_digest,
        })


@dataclass(frozen=True, slots=True)
class SbomEntry:
    extension_id: str
    component: str
    version: str
    artifact_digest: str
    source: str
    license: str = "unknown"

    def __post_init__(self) -> None:
        for value, field in ((self.extension_id, "extension_id"), (self.component, "component"), (self.version, "version"), (self.source, "source"), (self.license, "license")):
            _text(value, field)
        _sha256(self.artifact_digest, "artifact_digest")


@dataclass(frozen=True, slots=True)
class SbomInventory:
    entries: tuple[SbomEntry, ...]
    digest: str

    @classmethod
    def build(cls, entries: Iterable[SbomEntry]) -> "SbomInventory":
        ordered = tuple(sorted(entries, key=lambda item: (item.extension_id, item.component, item.version, item.artifact_digest)))
        keys = [(item.extension_id, item.component) for item in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("SBOM component identities must be unique per extension")
        material = [
            (item.extension_id, item.component, item.version, item.artifact_digest, item.source, item.license)
            for item in ordered
        ]
        return cls(ordered, _digest(material))

    def verify(self) -> None:
        rebuilt = self.build(self.entries)
        if rebuilt.digest != self.digest:
            raise ValueError("SBOM digest mismatch")


@dataclass(frozen=True, slots=True)
class ExtensionHealth:
    extension_id: str
    state: ExtensionHealthState
    reasons: tuple[str, ...] = ()
    quarantine: QuarantineDecision | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceInventory:
    records: tuple[ExtensionProvenance, ...]
    sbom: SbomInventory
    digest: str

    @classmethod
    def build(cls, records: Iterable[ExtensionProvenance], *, sbom: SbomInventory | None = None) -> "ProvenanceInventory":
        ordered = tuple(sorted(records, key=lambda item: (item.extension_id, item.version, item.artifact_digest)))
        ids = [(item.extension_id, item.version) for item in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("extension provenance identities must be unique")
        inventory = sbom or SbomInventory.build(
            SbomEntry(item.extension_id, item.extension_id, item.version, item.artifact_digest, item.source)
            for item in ordered
        )
        inventory.verify()
        material = {
            "records": [
                (item.extension_id, item.version, item.source, item.artifact_digest, item.manifest_digest,
                 item.signature.signer, item.signature.algorithm, item.signature.signature,
                 item.trust.source, item.trust.level.value, item.trust.basis, item.trust.issuer)
                for item in ordered
            ],
            "sbom": inventory.digest,
        }
        return cls(ordered, inventory, _digest(material))

    def verify(self) -> None:
        rebuilt = self.build(self.records, sbom=self.sbom)
        if rebuilt.digest != self.digest:
            raise ValueError("provenance inventory digest mismatch")

    def verify_signatures(self, verifier: Callable[[bytes, SignatureRecord], bool]) -> None:
        for record in self.records:
            if not verifier(record.signing_material(), record.signature):
                raise ValueError(f"signature rejected for {record.extension_id}")

    def record(self, extension_id: str) -> ExtensionProvenance:
        for item in self.records:
            if item.extension_id == extension_id:
                return item
        raise KeyError(extension_id)


class ExtensionProvenanceAdmission:
    """Bind provenance verification, compatibility quarantine, and health."""

    def __init__(self, inventory: ProvenanceInventory, quarantine: QuarantineRegistry | None = None) -> None:
        self._inventory = inventory
        self._quarantine = quarantine or QuarantineRegistry()

    def evaluate(
        self,
        manifest: ExtensionManifest,
        *,
        protocol: str,
        available_dependencies: set[str],
        granted_permissions: set[str],
        signatures_verified: bool = False,
    ) -> ExtensionHealth:
        provenance = self._inventory.record(manifest.extension_id)
        if not signatures_verified or provenance.trust.level is TrustLevel.UNTRUSTED:
            return ExtensionHealth(manifest.extension_id, ExtensionHealthState.UNVERIFIED, ("provenance-unverified",))
        decision = self._quarantine.evaluate(
            manifest, protocol=protocol, available_dependencies=available_dependencies,
            granted_permissions=granted_permissions,
        )
        if decision.quarantined:
            return ExtensionHealth(manifest.extension_id, ExtensionHealthState.QUARANTINED, decision.reasons, decision)
        return ExtensionHealth(manifest.extension_id, ExtensionHealthState.HEALTHY, (), decision)


__all__ = [
    "ExtensionHealth", "ExtensionHealthState", "ExtensionProvenance", "ExtensionProvenanceAdmission",
    "ProvenanceInventory", "SbomEntry", "SbomInventory", "SignatureRecord", "TrustLevel", "TrustRecord",
]
