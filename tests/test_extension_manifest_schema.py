"""Compatibility contract for ExtensionManifest's public wire schema.

``to_dict``/``from_dict`` are the one canonical serialization every adapter
(persistence, MCP/HTTP capability discovery) must share -- this file pins the
round trip and the backward-compatible defaults a schema-version bump must
preserve.
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.extensions.manifest import (
    CleanupPolicy,
    ExtensionDependency,
    ExtensionHealth,
    ExtensionIdentity,
    ExtensionManifest,
    ExtensionResources,
    HealthMode,
    MANIFEST_SCHEMA_VERSION,
)


def _manifest() -> ExtensionManifest:
    return ExtensionManifest(
        ExtensionIdentity("worker", "sonder"), "1.2.3", "extension-v1",
        dependencies=(ExtensionDependency("sonder.base", "2.0.0", True),),
        permissions=("network", "filesystem"),
        health=ExtensionHealth(HealthMode.REQUIRED, crash_limit=5, probe_timeout_ms=2000),
        cleanup=CleanupPolicy(on_quarantine="unload", retain_state=False),
        resources=ExtensionResources(64 * 1024 * 1024),
    )


def test_to_dict_reports_current_schema_version():
    assert _manifest().to_dict()["schema_version"] == MANIFEST_SCHEMA_VERSION


def test_round_trip_reconstructs_an_identical_manifest():
    manifest = _manifest()
    restored = ExtensionManifest.from_dict(manifest.to_dict())
    assert restored == manifest
    assert restored.digest() == manifest.digest()


def test_from_dict_accepts_a_pre_schema_version_dict():
    """A manifest persisted before schema_version existed still loads."""
    legacy = _manifest().to_dict()
    del legacy["schema_version"]
    assert ExtensionManifest.from_dict(legacy) == _manifest()


def test_from_dict_defaults_optional_sections_like_the_constructor():
    minimal = {
        "identity": {"name": "worker", "publisher": "sonder"},
        "version": "1.0.0",
        "protocol": "extension-v1",
    }
    restored = ExtensionManifest.from_dict(minimal)
    assert restored == ExtensionManifest(ExtensionIdentity("worker", "sonder"), "1.0.0", "extension-v1")


def test_digest_is_stable_across_a_schema_version_change():
    """Digest binds provenance to manifest *content*, not wire representation."""
    manifest = _manifest()
    newer_wire = dict(manifest.to_dict(), schema_version="2")
    assert ExtensionManifest.from_dict(newer_wire).digest() == manifest.digest()


def test_from_dict_rejects_a_non_mapping():
    with pytest.raises(TypeError):
        ExtensionManifest.from_dict(["not", "a", "manifest"])


def test_from_dict_rejects_missing_identity():
    with pytest.raises(KeyError):
        ExtensionManifest.from_dict({"version": "1.0.0", "protocol": "extension-v1"})
