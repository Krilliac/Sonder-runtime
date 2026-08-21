"""Production composition tests for bounded extension services."""
from __future__ import annotations

import sys
import inspect

import pytest

from sonder_runtime.application.extensions.experiments import (
    ExperimentStartupDenied,
    ExperimentState,
)
from sonder_runtime.application.extensions.registry import ExtensionRegistry
from sonder_runtime.bootstrap import app as bootstrap_app


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
