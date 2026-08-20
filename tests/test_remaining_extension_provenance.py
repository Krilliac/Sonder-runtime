from hashlib import sha256

import pytest

from sonder_runtime.application.extensions.provenance_inventory import (
    ExtensionHealthState,
    ExtensionProvenance,
    ExtensionProvenanceAdmission,
    ProvenanceInventory,
    SbomEntry,
    SbomInventory,
    SignatureRecord,
    TrustLevel,
    TrustRecord,
)
from sonder_runtime.domain.extensions.manifest import (
    CleanupPolicy,
    ExtensionHealth as ManifestHealth,
    ExtensionIdentity,
    ExtensionManifest,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _record(extension_id: str = "sonder.search", *, trust: TrustLevel = TrustLevel.TRUSTED) -> ExtensionProvenance:
    manifest_digest = _hash(extension_id + ":manifest")
    return ExtensionProvenance(
        extension_id, "1.2.3", "signed-registry", _hash(extension_id + ":artifact"), manifest_digest,
        SignatureRecord("release-key", "ed25519", "signature", manifest_digest),
        TrustRecord("signed-registry", trust, "operator-approved", "sonder-release"),
    )


def test_signature_source_and_trust_are_retained_and_verifiable():
    inventory = ProvenanceInventory.build([_record()])
    inventory.verify()
    inventory.verify_signatures(lambda material, signature: material and signature.signer == "release-key")
    record = inventory.record("sonder.search")
    assert record.source == "signed-registry"
    assert record.trust.level is TrustLevel.TRUSTED


def test_sbom_is_sorted_deterministic_and_tamper_evident():
    entries = [
        SbomEntry("sonder.z", "z-lib", "2", _hash("z"), "registry", "MIT"),
        SbomEntry("sonder.a", "a-lib", "1", _hash("a"), "registry", "Apache-2.0"),
    ]
    first = SbomInventory.build(entries)
    second = SbomInventory.build(reversed(entries))
    assert first.digest == second.digest
    assert [item.extension_id for item in first.entries] == ["sonder.a", "sonder.z"]
    tampered = SbomInventory(
        (SbomEntry("sonder.a", "a-lib", "9", _hash("a"), "registry"), first.entries[1]),
        first.digest,
    )
    with pytest.raises(ValueError, match="SBOM digest"):
        tampered.verify()


def _manifest() -> ExtensionManifest:
    return ExtensionManifest(
        ExtensionIdentity("search", "sonder"), "1.2.3", "extension-v1",
        health=ManifestHealth(crash_limit=2), cleanup=CleanupPolicy("disable", True),
    )


def test_compatibility_failure_becomes_quarantine_health():
    inventory = ProvenanceInventory.build([_record()])
    health = ExtensionProvenanceAdmission(inventory).evaluate(
        _manifest(), protocol="extension-v2", available_dependencies=set(), granted_permissions=set(),
        signatures_verified=True,
    )
    assert health.state is ExtensionHealthState.QUARANTINED
    assert "protocol-incompatible" in health.reasons
    assert health.quarantine is not None


def test_untrusted_or_unverified_provenance_fails_closed_before_admission():
    inventory = ProvenanceInventory.build([_record(trust=TrustLevel.UNTRUSTED)])
    health = ExtensionProvenanceAdmission(inventory).evaluate(
        _manifest(), protocol="extension-v1", available_dependencies=set(), granted_permissions=set(),
        signatures_verified=True,
    )
    assert health.state is ExtensionHealthState.UNVERIFIED
    assert health.reasons == ("provenance-unverified",)
