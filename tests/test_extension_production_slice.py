"""Focused production integration coverage for durable extension state."""
from __future__ import annotations

from hashlib import sha256
import sys

from sonder_runtime.adapters.extensions.host import ExtensionHost, ExtensionHostCrashed, ExtensionHostLimits
from sonder_runtime.adapters.persistence.sqlite.extensions import SQLiteExtensionStateRepository
from sonder_runtime.application.extensions.provenance_inventory import (
    ExtensionProvenance, ProvenanceInventory, SignatureRecord, TrustLevel, TrustRecord,
)
from sonder_runtime.application.extensions.registry import ExtensionHealthState, ExtensionRegistry
from sonder_runtime.application.extensions.facade import ExtensionAuthority
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.extensions.manifest import ExtensionHealth, ExtensionIdentity, ExtensionManifest


def _manifest(version="1.0.0", crash_limit=2):
    return ExtensionManifest(ExtensionIdentity("worker", "sonder"), version, "extension-v1",
                             health=ExtensionHealth(crash_limit=crash_limit))


def _trusted(manifest):
    digest = manifest.digest()
    return ProvenanceInventory.build([ExtensionProvenance(
        manifest.extension_id, manifest.version, "signed-registry", sha256(b"artifact").hexdigest(), digest,
        SignatureRecord("release", "ed25519", "sig", digest),
        TrustRecord("signed-registry", TrustLevel.TRUSTED, "operator-approved"),
    )])


def test_sqlite_registry_state_survives_reconstruction_and_retains_quarantine(tmp_path):
    manifest = _manifest(crash_limit=1)
    repository = SQLiteExtensionStateRepository(tmp_path / "extensions.db")
    registry = ExtensionRegistry(provenance=_trusted(manifest), repository=repository)
    registry.install(manifest, scope="global", signatures_verified=True)
    record = registry.record_crash(manifest.extension_id, scope="global")
    assert record.health_state is ExtensionHealthState.QUARANTINED

    restored = ExtensionRegistry(provenance=_trusted(manifest), repository=repository)
    restored_record = restored.get(manifest.extension_id, scope="global")
    assert restored_record.health_state is ExtensionHealthState.QUARANTINED
    assert restored_record.enabled is False
    assert restored_record.crash_count == 1


def test_production_composition_uses_durable_fail_closed_registry(tmp_path, monkeypatch):
    bootstrap_app.reset_for_tests()
    monkeypatch.setenv("SONDER_EXTENSIONS_DB", str(tmp_path / "extensions.db"))
    application = bootstrap_app.build_application()
    try:
        assert application.extension_facade().registry_health(
            ExtensionAuthority("test", frozenset({"registry_health"}))
        ).persistence == "durable"
    finally:
        bootstrap_app.reset_for_tests()


def test_host_restart_budget_remains_bounded_after_child_crash():
    source = 'import json,sys; print(json.dumps({"type":"ready"}), flush=True); next(sys.stdin); raise SystemExit(7)'
    host = ExtensionHost([sys.executable, "-c", source], limits=ExtensionHostLimits(max_restarts=1, max_crashes=1))
    try:
        try:
            host.call("crash")
        except ExtensionHostCrashed:
            pass
        assert host.stats.restarts <= 1
        assert host.stats.launches <= 2
    finally:
        host.close()
