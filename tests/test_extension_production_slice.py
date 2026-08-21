"""Focused production integration coverage for durable extension state."""
from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
import sys

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost, ExtensionHostCrashed, ExtensionHostLimits
from sonder_runtime.adapters.persistence.sqlite.extensions import SQLiteExtensionStateRepository
from sonder_runtime.application.extensions.provenance_inventory import (
    ExtensionProvenance, ProvenanceInventory, SignatureRecord, TrustLevel, TrustRecord,
)
from sonder_runtime.application.extensions.registry import ExtensionHealthState, ExtensionRegistry
from sonder_runtime.application.extensions.facade import ExtensionAuthority
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.extensions.manifest import ExtensionHealth, ExtensionIdentity, ExtensionManifest
from sonder_runtime.domain.extensions.artifact import ExtensionArtifactReceipt


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


def test_sqlite_registry_persists_verified_artifact_receipt(tmp_path):
    manifest = _manifest()
    repository = SQLiteExtensionStateRepository(tmp_path / "extensions.db")
    registry = ExtensionRegistry(provenance=_trusted(manifest), repository=repository)
    receipt = ExtensionArtifactReceipt(
        "C:/staging/worker.pkg", sha256(b"artifact").hexdigest(), 8,
        "https://example.test/worker.pkg",
    )
    registry.install_verified(manifest, receipt, scope="global", signatures_verified=True)
    restored = ExtensionRegistry(provenance=_trusted(manifest), repository=repository)
    record = restored.get(manifest.extension_id, scope="global")
    assert record.artifact == receipt


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


def test_production_health_exposes_empty_provenance_inventory_fail_closed(tmp_path, monkeypatch):
    bootstrap_app.reset_for_tests()
    monkeypatch.setenv("SONDER_EXTENSIONS_DB", str(tmp_path / "extensions.db"))
    application = bootstrap_app.build_application()
    try:
        health = application.extension_facade().registry_health(
            ExtensionAuthority("test", frozenset({"registry_health"}))
        )
        assert health.provenance_records == 0
        assert health.provenance_digest
        assert application.extension_registry().provenance_inventory.records == ()
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


def test_sqlite_registry_rejects_tampered_duplicated_record_fields(tmp_path):
    manifest = _manifest()
    repository = SQLiteExtensionStateRepository(tmp_path / "extensions.db")
    registry = ExtensionRegistry(provenance=_trusted(manifest), repository=repository)
    registry.install(manifest, scope="global", signatures_verified=True)
    with sqlite3.connect(str(tmp_path / "extensions.db")) as connection:
        row = connection.execute("SELECT slot, record_json FROM extension_registry_state").fetchone()
        payload = json.loads(row[1])
        payload["version"] = "9.9.9"
        connection.execute(
            "UPDATE extension_registry_state SET record_json = ? WHERE slot = ?",
            (json.dumps(payload), row[0]),
        )
    with pytest.raises(ValueError, match="version mismatch"):
        SQLiteExtensionStateRepository(tmp_path / "extensions.db").load()
