"""Application and interface exposure tests for EXT-004 through EXT-007."""
from __future__ import annotations

from io import StringIO
import json
import sys

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost
from sonder_runtime.application.extensions.experiments import (
    EphemeralExperimentManager,
    ExperimentStartupDenied,
)
from sonder_runtime.application.extensions.facade import (
    ExtensionApplicationFacade,
    ExtensionAuthority,
    ExtensionAuthorityDenied,
)
from sonder_runtime.application.extensions.registry import ExtensionRegistry
from sonder_runtime.domain.extensions.manifest import ExtensionHealth, ExtensionIdentity, ExtensionManifest
from sonder_runtime.interfaces.cli.extensions import ExtensionCommand
from sonder_runtime.interfaces.http.facades.extensions import dispatch_extension_route


SERVER = 'import json,sys; print(json.dumps({"type":"ready"}), flush=True); sys.stdin.read()'


def _authority(*operations: str) -> ExtensionAuthority:
    return ExtensionAuthority("test-admin", frozenset(operations))


def _facade(*, allow_start: bool = True):
    def factory(definition, directory):
        return ExtensionHost(definition.argv, cwd=directory)

    manager = EphemeralExperimentManager(
        lambda definition: allow_start,
        host_factory=factory,
    )
    return ExtensionApplicationFacade(ExtensionRegistry(), manager), manager


def test_facade_requires_explicit_typed_authority_for_every_operation():
    facade, manager = _facade()
    try:
        with pytest.raises(ExtensionAuthorityDenied):
            facade.registry_health(_authority("inspect"))
        with pytest.raises(ExtensionAuthorityDenied):
            facade.define("trial", (sys.executable, "-c", SERVER), authority=_authority("inspect"))
    finally:
        manager.close()


def test_facade_exposes_complete_ephemeral_lifecycle_without_promotion():
    facade, manager = _facade()
    authority = _authority("define", "inspect", "start", "stop", "delete")
    try:
        defined = facade.define("trial", (sys.executable, "-c", SERVER), authority=authority)
        assert facade.inspect("trial", authority).state == "defined"
        assert facade.start("trial", authority).state == "running"
        assert facade.stop("trial", authority).state == "stopped"
        assert facade.delete("trial", authority).state == "deleted"
        health = facade.registry_health(_authority("registry_health"))
        assert health.persistence == "in-memory-only"
        assert health.promotion == "not-supported"
        assert defined.experiment_id == "trial"
    finally:
        manager.close()


def test_facade_preserves_bootstrap_startup_fail_closed():
    facade, manager = _facade(allow_start=False)
    authority = _authority("define", "start", "inspect")
    try:
        facade.define("denied", (sys.executable, "-c", SERVER), authority=authority)
        with pytest.raises(ExperimentStartupDenied):
            facade.start("denied", authority)
        assert facade.inspect("denied", authority).state == "defined"
    finally:
        manager.close()


def test_registry_health_exposes_quarantine_and_repair_diagnostics():
    facade, manager = _facade()
    manifest = ExtensionManifest(
        ExtensionIdentity("search", "sonder"), "1.0.0", "extension-v1",
        health=ExtensionHealth(crash_limit=1),
    )
    # The registry remains the bounded in-memory authority; this simulates
    # lifecycle crash reports arriving from the host boundary.
    facade._registry.install(manifest, scope="global")
    facade._registry.record_crash(manifest.extension_id, scope="global")
    try:
        health = facade.registry_health(_authority("registry_health"))
        record = health.snapshot.records[0]
        assert record.quarantine is not None
        assert record.health_state.value == "quarantined"
        assert health.diagnostics[0].recommended_action == "inspect-quarantine"
    finally:
        manager.close()


def test_http_facade_dispatches_health_and_lifecycle():
    facade, manager = _facade()
    authority = _authority("registry_health", "define", "inspect", "delete")
    try:
        health = dispatch_extension_route(facade, "GET", "/v1/extensions", None, authority)
        assert health is not None and health.status_code == 200
        assert health.body["persistence"] == "in-memory-only"
        defined = dispatch_extension_route(
            facade, "POST", "/v1/extensions/experiments/define",
            {"experiment_id": "http-trial", "argv": [sys.executable, "-c", SERVER]}, authority,
        )
        assert defined is not None and defined.status_code == 201
        inspected = dispatch_extension_route(
            facade, "GET", "/v1/extensions/experiments/http-trial/inspect", None, authority,
        )
        assert inspected is not None and inspected.body["experiment"]["state"] == "defined"
        assert dispatch_extension_route(facade, "GET", "/v1/not-extensions", None, authority) is None
    finally:
        manager.close()


def test_http_registry_update_preserves_declared_resource_budget():
    facade, manager = _facade()
    authority = _authority("update", "registry_health")
    try:
        updated = dispatch_extension_route(
            facade, "POST", "/v1/extensions/registry/update",
            {"manifest": {
                "extension_id": "sonder.limited",
                "version": "1.0.0",
                "protocol": "extension-v1",
                "memory_limit_bytes": 256 * 1024 * 1024,
            }}, authority,
        )
        assert updated is not None and updated.status_code == 200
        assert updated.body["extension"]["resources"]["memory_limit_bytes"] == 256 * 1024 * 1024
    finally:
        manager.close()


def test_cli_facade_emits_machine_readable_health_and_requires_injected_authority():
    facade, manager = _facade()
    try:
        out = StringIO()
        status = ExtensionCommand(facade).run(
            ["health"], authority=_authority("registry_health"), out=out,
        )
        assert status == 0
        body = json.loads(out.getvalue())
        assert body["object"] == "extension_registry_health"
        assert body["persistence"] == "in-memory-only"

        denied = StringIO()
        status = ExtensionCommand(facade).run(
            ["health"], authority=_authority("inspect"), out=denied,
        )
        assert status == 1
        assert json.loads(denied.getvalue())["type"] == "ExtensionAuthorityDenied"
    finally:
        manager.close()
