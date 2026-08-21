from hashlib import sha256

import pytest

from sonder_runtime.application.extensions.provenance_inventory import (
    ExtensionProvenance,
    ExtensionProvenanceAdmission,
    ProvenanceInventory,
    SignatureRecord,
    TrustLevel,
    TrustRecord,
)
from sonder_runtime.application.extensions.registry import (
    ExtensionAlreadyInstalledError,
    ExtensionHealthState,
    ExtensionRegistry,
    ExtensionRegistryError,
    ExtensionScope,
)
from sonder_runtime.domain.extensions.manifest import ExtensionHealth, ExtensionIdentity, ExtensionManifest
from sonder_runtime.domain.extensions.artifact import ExtensionArtifactReceipt


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _manifest(name: str = "search", version: str = "1.2.3", *, crash_limit: int = 2) -> ExtensionManifest:
    return ExtensionManifest(
        ExtensionIdentity(name, "sonder"), version, "extension-v1",
        health=ExtensionHealth(crash_limit=crash_limit),
    )


def _inventory(manifest: ExtensionManifest) -> ProvenanceInventory:
    digest = manifest.digest()
    return ProvenanceInventory.build([ExtensionProvenance(
        manifest.extension_id, manifest.version, "signed-registry", _hash("artifact"), digest,
        SignatureRecord("release-key", "ed25519", "signature", digest),
        TrustRecord("signed-registry", TrustLevel.TRUSTED, "operator-approved"),
    )])


def _registry(manifest: ExtensionManifest) -> ExtensionRegistry:
    return ExtensionRegistry(provenance=_inventory(manifest))


def test_project_and_global_slots_are_typed_and_snapshot_sorted():
    manifest = _manifest()
    registry = _registry(manifest)
    global_record = registry.install(manifest, scope="global", signatures_verified=True)
    project_record = registry.install(manifest, scope=ExtensionScope.PROJECT, project_id="project-b", signatures_verified=True)

    assert global_record.enabled and global_record.healthy
    assert project_record.project_id == "project-b"
    snapshot = registry.snapshot()
    assert [record.key for record in snapshot] == [
        ("global", "", "sonder.search"), ("project", "project-b", "sonder.search")
    ]
    assert snapshot.digest == registry.snapshot().digest


def test_install_rejects_duplicate_and_invalid_scope_without_mutation():
    manifest = _manifest()
    registry = _registry(manifest)
    registry.install(manifest, scope="global", signatures_verified=True)
    with pytest.raises(ExtensionAlreadyInstalledError):
        registry.install(manifest, scope="global", signatures_verified=True)
    with pytest.raises(ExtensionRegistryError):
        registry.install(manifest, scope="project", signatures_verified=True)
    assert len(registry.snapshot()) == 1


def test_missing_provenance_fails_closed_and_diagnostic_is_deterministic():
    registry = ExtensionRegistry(provenance=ProvenanceInventory.build([]))
    record = registry.install(_manifest(), scope="global", signatures_verified=True)
    assert record.health_state is ExtensionHealthState.UNVERIFIED
    assert not record.enabled
    diagnostic = registry.repair_diagnostics()[0]
    assert diagnostic.codes == ("provenance-missing",)
    assert diagnostic.recommended_action == "review-provenance"


def test_verified_artifact_admission_binds_digest_and_rejects_tampering():
    manifest = _manifest()
    registry = _registry(manifest)
    receipt = ExtensionArtifactReceipt("C:/staging/search.pkg", _hash("artifact"), 8, "https://example.test/search.pkg")
    installed = registry.install_verified(
        manifest, receipt, scope="global", signatures_verified=True,
    )
    assert installed.artifact == receipt
    with pytest.raises(ExtensionRegistryError, match="artifact digest"):
        registry.install_verified(
            _manifest(version="1.2.4"),
            ExtensionArtifactReceipt("C:/staging/search-new.pkg", _hash("wrong"), 5),
            scope="global", signatures_verified=True,
        )


def test_disable_enable_and_quarantine_from_repeated_crash():
    manifest = _manifest(crash_limit=2)
    registry = _registry(manifest)
    registry.install(manifest, scope="global", signatures_verified=True)
    assert not registry.disable("sonder.search", scope="global").enabled
    assert registry.enable("sonder.search", scope="global").enabled
    assert not registry.record_crash("sonder.search", scope="global").health_state is ExtensionHealthState.QUARANTINED
    quarantined = registry.record_crash("sonder.search", scope="global")
    assert quarantined.health_state is ExtensionHealthState.QUARANTINED
    assert not quarantined.enabled
    assert registry.enable("sonder.search", scope="global").enabled is False


def test_update_replaces_version_in_place_and_preserves_slot():
    first = _manifest()
    second = _manifest(version="1.2.4")
    registry = ExtensionRegistry(provenance=_inventory(first))
    registry.install(first, scope="project", project_id="alpha", signatures_verified=True)
    updated = registry.update(second, scope="project", project_id="alpha", signatures_verified=True)
    assert updated.version == "1.2.4"
    assert updated.project_id == "alpha"
    assert len(registry.snapshot()) == 1


def test_registry_does_not_mutate_external_manifest_or_provenance_state():
    manifest = _manifest()
    inventory = _inventory(manifest)
    registry = ExtensionRegistry(provenance=inventory)
    before = inventory.digest
    record = registry.install(manifest, scope="global", signatures_verified=True)
    assert record.manifest is manifest
    assert inventory.digest == before
    assert registry.snapshot().records[0].manifest_digest == manifest.digest()
