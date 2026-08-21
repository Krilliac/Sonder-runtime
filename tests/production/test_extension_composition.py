"""Production composition tests for bounded extension services."""
from __future__ import annotations

import sys
import inspect
from io import StringIO
import json

import pytest

from sonder_runtime.application.extensions.experiments import (
    ExperimentStartupDenied,
    ExperimentState,
)
from sonder_runtime.application.extensions.registry import ExtensionRegistry
from sonder_runtime.application.extensions.facade import ExtensionApplicationFacade
from sonder_runtime.domain.extensions.manifest import ExtensionIdentity, ExtensionManifest
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.application.extensions.facade import ExtensionAuthority
from sonder_runtime.interfaces.cli.extensions import ExtensionCommand
from sonder_runtime.interfaces.http.facades.extensions import dispatch_extension_route


pytestmark = pytest.mark.integration


@pytest.fixture()
def application():
    bootstrap_app.reset_for_tests()
    try:
        yield bootstrap_app.build_application()
    finally:
        bootstrap_app.reset_for_tests()


def _server():
    return [
        sys.executable,
        "-c",
        'import json,sys; print(json.dumps({"type":"ready"}), flush=True); '
        'sys.stdin.read()',
    ]


def test_extension_services_are_lazy_singletons_and_do_not_change_legacy_shape(application):
    assert application.extension_registry is not None
    assert application.experiment_manager is not None
    assert application.extension_registry() is application.extension_registry()
    assert application.experiment_manager() is application.experiment_manager()
    assert isinstance(application.extension_registry(), ExtensionRegistry)
    assert application.extension_facade() is application.extension_facade()
    assert isinstance(application.extension_facade(), ExtensionApplicationFacade)


def test_experiment_manager_denies_startup_without_explicit_authority(application):
    manager = application.experiment_manager()
    try:
        manager.define("default-deny", _server())
        with pytest.raises(ExperimentStartupDenied):
            manager.start("default-deny")
        assert manager.inspect("default-deny").state == ExperimentState.DEFINED
    finally:
        manager.close()


def test_explicit_startup_authority_reaches_injected_host_boundary():
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application(
        extension_startup_authority=lambda definition: definition.experiment_id == "allowed",
    )
    manager = application.experiment_manager()
    try:
        manager.define("allowed", _server())
        assert manager.start("allowed").state == ExperimentState.RUNNING
        assert manager.stop("allowed").state == ExperimentState.STOPPED
    finally:
        manager.close()
        bootstrap_app.reset_for_tests()


def test_application_extension_services_do_not_import_adapters():
    from sonder_runtime.application.extensions import experiments, registry

    assert "sonder_runtime.adapters" not in inspect.getsource(experiments)
    assert "sonder_runtime.adapters" not in inspect.getsource(registry)
    assert "..adapters" not in inspect.getsource(experiments)
    assert "..adapters" not in inspect.getsource(registry)


def test_live_application_extension_registry_persists_and_rehydrates_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    manifest = ExtensionManifest(
        ExtensionIdentity("persisted", "sonder"), "1.0.0", "extension-v1"
    )
    try:
        first = bootstrap_app.build_application()
        installed = first.extension_registry().install(
            manifest, scope="global", protocol="extension-v1",
        )
        assert first.extension_registry().durable is True
        assert installed.manifest_digest == manifest.digest()
        first_digest = first.extension_registry().snapshot().digest
    finally:
        bootstrap_app.reset_for_tests()


    try:
        second = bootstrap_app.build_application()
        restored = second.extension_registry().get("sonder.persisted", scope="global")
        assert restored.manifest_digest == manifest.digest()
        assert second.extension_registry().snapshot().digest == first_digest
        # Missing provenance remains explicit; persistence must not turn an
        # unverified installation into an executable healthy extension.
        assert restored.enabled is False
    finally:
        bootstrap_app.reset_for_tests()


def test_live_durable_registry_is_reachable_through_http_and_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    manifest = ExtensionManifest(
        ExtensionIdentity("operator", "sonder"), "1.0.0", "extension-v1"
    )
    authority = ExtensionAuthority(
        "operator", frozenset({"registry_health", "disable"})
    )
    try:
        application = bootstrap_app.build_application()
        application.extension_registry().install(manifest, scope="global")
        health = dispatch_extension_route(
            application.extension_facade(), "GET", "/v1/extensions", None, authority
        )
        assert health is not None and health.status_code == 200
        assert health.body["persistence"] == "durable"
        disabled = dispatch_extension_route(
            application.extension_facade(), "POST",
            "/v1/extensions/registry/sonder.operator/disable",
            {"scope": "global"}, authority,
        )
        assert disabled is not None and disabled.status_code == 200
        assert disabled.body["extension"]["enabled"] is False

        out = StringIO()
        assert ExtensionCommand(application.extension_facade()).run(
            ["health"], authority=authority, out=out
        ) == 0
        cli_health = json.loads(out.getvalue())
        assert cli_health["persistence"] == "durable"
        assert cli_health["records"][0]["enabled"] is False
    finally:
        bootstrap_app.reset_for_tests()

    try:
        restored = bootstrap_app.build_application().extension_registry().get(
            "sonder.operator", scope="global"
        )
        assert restored.enabled is False
    finally:
        bootstrap_app.reset_for_tests()
