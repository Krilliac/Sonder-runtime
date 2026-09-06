"""Bound the explicit control-state rehearsal composition seam.

The provider is deliberately constructed only by the factory under test.  These
tests keep ordinary application composition separate from that opt-in path and
exercise direct ``SonderConfig`` construction, which bypasses ``load_config``.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
import sys

import pytest

from sonder_runtime.adapters.cluster.http_control_state import (
    HttpsControlStateProvider,
)
from sonder_runtime.application.control_state import ExternalControlStateCoordinator
from sonder_runtime.bootstrap.control_state_rehearsal import (
    build_control_state_rehearsal,
)
from sonder_runtime.domain.cluster_availability import (
    CONTROL_STATE_PROTOCOL_VERSION,
    PartitionState,
)
from sonder_runtime.platform.config import (
    ComputeConfig,
    ComputeNodeConfig,
    DeploymentConfig,
    Secrets,
    SonderConfig,
)
from sonder_runtime.platform.control_state_rehearsal_config import (
    ControlStateRehearsalConfig,
)


def _enabled_pooled_pair_config() -> SonderConfig:
    return SonderConfig(
        profile="server-private",
        compute=ComputeConfig(
            allow_remote=True,
            node_id="node-a",
            nodes=(ComputeNodeConfig(node_id="node-b"),),
        ),
        deployment=DeploymentConfig(
            profile="pooled-pair",
            preferred_primary="node-a",
        ),
        secrets=Secrets(control_state_rehearsal_key="rehearsal-only-key"),
        control_state_rehearsal=ControlStateRehearsalConfig(
            enabled=True,
            cluster_id="rehearsal-cluster-a",
            node_id="node-a",
            witness_id="witness-a",
            provider_id="provider-a",
            origin="https://control-state.example.test",
            timeout_seconds=7,
        ),
    )


def _replace_rehearsal(config: SonderConfig, **changes: object) -> SonderConfig:
    return replace(
        config,
        control_state_rehearsal=replace(config.control_state_rehearsal, **changes),
    )


def _provider_counter(monkeypatch):
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    calls: list[dict[str, object]] = []

    def construct(**kwargs):
        calls.append(kwargs)
        raise AssertionError("provider must not be constructed for invalid rehearsal config")

    monkeypatch.setattr(rehearsal, "HttpsControlStateProvider", construct)
    return calls


@pytest.fixture
def _isolated_application_process() -> None:
    """Keep normal composition tests away from the user's runtime state."""
    import sonder_runtime.bootstrap.app as bootstrap_app
    from sonder_runtime.platform import paths

    environment = dict(os.environ)
    argv = list(sys.argv)
    bootstrap_app.reset_for_tests()
    paths.reset_home()
    try:
        yield
    finally:
        bootstrap_app.reset_for_tests()
        paths.reset_home()
        os.environ.clear()
        os.environ.update(environment)
        sys.argv[:] = argv


def _write_pooled_pair_toml(tmp_path, *, rehearsal_enabled: bool):
    config_path = tmp_path / "rehearsal.toml"
    state_home = tmp_path / "state"
    config_path.write_text(
        "\n".join(
            (
                'profile = "server-private"',
                "",
                "[state]",
                f"home = {json.dumps(str(state_home))}",
                "workspace_roots = []",
                "",
                "[compute]",
                "allow_remote = true",
                'node_id = "node-a"',
                "nodes = [{ "
                'id = "node-b", '
                'origin = "https://node-b.example.test:8443", '
                'workloads = ["build"], '
                'capabilities = ["cpu"], '
                'workspace_mappings = ["default"] '
                "}]",
                "",
                "[deployment]",
                'profile = "pooled-pair"',
                'preferred_primary = "node-a"',
                "",
                "[control_state_rehearsal]",
                f"enabled = {'true' if rehearsal_enabled else 'false'}",
                'cluster_id = "rehearsal-cluster-a"',
                'node_id = "node-a"',
                'witness_id = "witness-a"',
                'provider_id = "provider-a"',
                'origin = "https://control-state.example.test"',
                "timeout_seconds = 7",
                "",
            )
        ),
        encoding="utf-8",
    )
    return config_path, state_home


def _install_ordinary_entrypoint_stubs(monkeypatch, observed: list[tuple[str, object]]):
    """Replace only terminal handoffs; keep parser/config/command routing real."""
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.adapters.persistence.migrations as migrations
    import sonder_runtime.adapters.persistence.operations_store as operations_store
    import sonder_runtime.adapters.persistence.sqlite.bridge_migration as bridge_migration
    import sonder_runtime.adapters.security.unsafe_lab as unsafe_lab
    import sonder_runtime.adapters.embeddings as embeddings
    import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
    import sonder_runtime.adapters.inference.ollama_pool as ollama_pool
    import sonder_runtime.adapters.inference.ollama_vision as ollama_vision
    import sonder_runtime.bootstrap.app as bootstrap_app
    import sonder_runtime.bootstrap.legacy_interfaces as legacy_interfaces
    import sonder_runtime.bootstrap.legacy_mcp as legacy_mcp
    import sonder_runtime.bootstrap.native_mcp as native_mcp
    import sonder_runtime.interfaces.http.serve as serve
    import sonder_runtime.interfaces.repl.repl as repl
    import sonder_runtime.platform.logging as runtime_logging

    class Application:
        memory = object()

        def close_providers(self, *, timeout: float) -> None:
            observed.append(("close", timeout))

    application = Application()

    def default_app(*, config=None):
        observed.append(("default-app", config))
        return application

    def build_application(*, config):
        observed.append(("build-application", config))
        return application

    class LegacyRuntime:
        def require_startup_safety(self) -> None:
            observed.append(("legacy-safety", None))

        def run(self, *, safety_checked: bool) -> None:
            observed.append(("legacy-run", safety_checked))

    class OperationsStore:
        def prune_events(self, _retention_days: int) -> int:
            return 0

    monkeypatch.setattr(entrypoint, "_configure_typed_home", lambda _config: None)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entrypoint, "build_legacy_server_mcp_runtime", LegacyRuntime)
    monkeypatch.setattr(runtime_logging, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_kwargs: {})
    monkeypatch.setattr(bridge_migration, "require_epoch_2", lambda _home: None)
    monkeypatch.setattr(operations_store, "OperationsStore", OperationsStore)
    monkeypatch.setattr(ollama_endpoint, "configure_typed_endpoint", lambda _url: None)
    monkeypatch.setattr(ollama_pool, "configure_typed_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ollama_vision, "configure_typed_request_timeout", lambda _timeout: None)
    monkeypatch.setattr(embeddings, "configure_typed_endpoint", lambda _url: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_interfaces", lambda: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_capacity", lambda **_kwargs: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_application", lambda _app: None)
    monkeypatch.setattr(legacy_mcp, "configure_legacy_application", lambda _app: None)
    monkeypatch.setattr(bootstrap_app, "default_app", default_app)
    monkeypatch.setattr(bootstrap_app, "build_application", build_application)
    monkeypatch.setattr(serve, "configure_typed_config", lambda config: observed.append(("serve-config", config)))
    monkeypatch.setattr(serve, "configure_thin_handlers", lambda _handlers: None)
    monkeypatch.setattr(serve, "main", lambda *, config: observed.append(("serve-main", config)))
    monkeypatch.setattr(repl, "main", lambda: observed.append(("repl", "text")))
    monkeypatch.setattr(repl, "run_jsonl", lambda: observed.append(("repl", "json")))
    monkeypatch.setattr(native_mcp, "run_native_mcp", lambda app, *, close_compute_on_exit: observed.append(("native-mcp", app)) or 0)
    monkeypatch.setattr(unsafe_lab, "require_startup", lambda: observed.append(("native-safety", None)))


def _entrypoint_arguments(case: str, config_path) -> list[str]:
    cases = {
        "serve": ["serve", "--config", str(config_path), "--skip-preflight"],
        "repl-text": ["repl", "--config", str(config_path)],
        "repl-json": ["repl", "--config", str(config_path), "--json"],
        "mcp-legacy": ["mcp", "--config", str(config_path)],
        "mcp-native": ["mcp", "--config", str(config_path), "--native"],
    }
    return cases[case]


def _set_rehearsal_test_environment(monkeypatch) -> None:
    """Use only the temporary TOML home, never an inherited test/runtime home."""
    monkeypatch.delenv("SONDER_HOME", raising=False)
    monkeypatch.delenv("SONDER_STATE_HOME", raising=False)
    monkeypatch.setenv("SONDER_API_KEY", "a" * 24)
    monkeypatch.setenv("SONDER_CONTROL_STATE_REHEARSAL_API_KEY", "rehearsal-key")


def test_disabled_rehearsal_refuses_before_provider_construction(monkeypatch) -> None:
    calls = _provider_counter(monkeypatch)

    with pytest.raises(ValueError, match="^control-state rehearsal is disabled$"):
        build_control_state_rehearsal(SonderConfig())

    assert calls == []


def test_non_exact_config_is_rejected_before_provider_construction(monkeypatch) -> None:
    calls = _provider_counter(monkeypatch)

    with pytest.raises(TypeError, match="^config must be a SonderConfig$"):
        build_control_state_rehearsal(object())

    assert calls == []


def test_valid_rehearsal_declares_exact_provider_contract() -> None:
    coordinator = build_control_state_rehearsal(_enabled_pooled_pair_config())

    assert isinstance(coordinator, ExternalControlStateCoordinator)
    assert isinstance(coordinator.provider, HttpsControlStateProvider)
    assert coordinator.minimum_data_replicas == 2
    assert coordinator.capabilities.provider_id == "provider-a"
    assert coordinator.capabilities.protocol_version == CONTROL_STATE_PROTOCOL_VERSION
    assert coordinator.capabilities.data_replica_ids == ("node-a", "node-b")
    assert coordinator.capabilities.witness_ids == ("witness-a",)
    assert coordinator.capabilities.durable_acknowledgements is True
    assert coordinator.capabilities.external_fencing is True
    assert coordinator.capabilities.partition_policy is PartitionState.SAFE


def test_explicit_loopback_rehearsal_requires_and_uses_adapter_opt_in() -> None:
    config = _replace_rehearsal(
        _enabled_pooled_pair_config(),
        origin="http://127.0.0.1:18080",
        allow_insecure_loopback=True,
    )

    coordinator = build_control_state_rehearsal(config)

    assert isinstance(coordinator.provider, HttpsControlStateProvider)
    assert coordinator.capabilities.data_replica_ids == ("node-a", "node-b")


def test_valid_rehearsal_passes_only_expected_values_to_provider(monkeypatch) -> None:
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    captured: list[dict[str, object]] = []

    class ProviderSpy:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)
            self.provider_id = kwargs["capabilities"].provider_id
            self.protocol_version = kwargs["capabilities"].protocol_version

        def append(self, event):
            raise AssertionError("factory must not append")

        def read(self, cluster_id, *, after_sequence, limit):
            raise AssertionError("factory must not read")

        def fence(self, ownership):
            raise AssertionError("factory must not fence")

    monkeypatch.setattr(rehearsal, "HttpsControlStateProvider", ProviderSpy)

    coordinator = build_control_state_rehearsal(_enabled_pooled_pair_config())

    assert isinstance(coordinator.provider, ProviderSpy)
    assert captured == [
        {
            "origin": "https://control-state.example.test",
            "api_key": "rehearsal-only-key",
            "capabilities": coordinator.capabilities,
            "timeout_seconds": 7,
            "allow_insecure_loopback": False,
        }
    ]


@pytest.mark.parametrize(
    "invalid_config",
    [
        pytest.param(
            lambda config: replace(
                config,
                deployment=replace(config.deployment, profile="single-host"),
            ),
            id="wrong-deployment-profile",
        ),
        pytest.param(
            lambda config: replace(
                config,
                compute=replace(config.compute, nodes=()),
            ),
            id="missing-peer",
        ),
        pytest.param(
            lambda config: replace(
                config,
                compute=replace(
                    config.compute,
                    nodes=(
                        ComputeNodeConfig(node_id="node-b"),
                        ComputeNodeConfig(node_id="node-c"),
                    ),
                ),
            ),
            id="extra-peer",
        ),
        pytest.param(
            lambda config: replace(
                config,
                compute=replace(
                    config.compute,
                    nodes=(ComputeNodeConfig(node_id="node-a"),),
                ),
            ),
            id="duplicate-peer",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(config, node_id="node-c"),
            id="local-node-mismatch",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(config, witness_id="node-b"),
            id="witness-collides-with-data",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(config, enabled=1),
            id="non-boolean-enabled",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(
                config,
                allow_insecure_loopback=1,
            ),
            id="non-boolean-loopback-opt-in",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(config, timeout_seconds=0),
            id="out-of-range-timeout",
        ),
        pytest.param(
            lambda config: replace(config, secrets=Secrets()),
            id="missing-dedicated-key",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(
                config,
                origin="http://control-state.example.test",
            ),
            id="plaintext-remote-origin",
        ),
        pytest.param(
            lambda config: _replace_rehearsal(
                config,
                origin="http://127.0.0.1:18080",
            ),
            id="plaintext-loopback-without-opt-in",
        ),
        pytest.param(
            lambda config: replace(
                config,
                compute=replace(config.compute, allow_remote=False),
            ),
            id="remote-compute-disabled",
        ),
        pytest.param(
            lambda config: replace(
                config,
                deployment=replace(config.deployment, automatic_takeover=True),
            ),
            id="automatic-takeover-enabled",
        ),
        pytest.param(
            lambda config: replace(
                config,
                deployment=replace(config.deployment, automatic_failback=True),
            ),
            id="automatic-failback-enabled",
        ),
    ],
)
def test_direct_invalid_rehearsal_config_fails_before_provider_construction(
    invalid_config,
    monkeypatch,
) -> None:
    calls = _provider_counter(monkeypatch)
    config = invalid_config(_enabled_pooled_pair_config())

    with pytest.raises(ValueError) as exc_info:
        build_control_state_rehearsal(config)

    assert "rehearsal-only-key" not in str(exc_info.value)
    assert calls == []


@pytest.mark.parametrize(
    "config_factory",
    [
        pytest.param(SonderConfig, id="disabled-rehearsal"),
        pytest.param(_enabled_pooled_pair_config, id="enabled-rehearsal"),
    ],
)
def test_ordinary_serve_mcp_and_repl_composition_roots_never_construct_provider(
    config_factory,
    monkeypatch,
) -> None:
    """Serve/REPL share ``default_app``; native MCP uses ``build_application``."""
    import sonder_runtime.bootstrap.app as bootstrap_app

    calls = _provider_counter(monkeypatch)
    config = config_factory()
    applications = []
    try:
        # The HTTP serve and interactive REPL commands each compose through this
        # default application root.  No listener or interactive loop is started.
        bootstrap_app.reset_for_tests()
        applications.append(bootstrap_app.default_app(config=config))
        # Native MCP composes a separate application graph through this public
        # root.  The transport itself is intentionally not launched here.
        applications.append(bootstrap_app.build_application(config=config))
    finally:
        for application in applications:
            application.close_providers(timeout=5)
        bootstrap_app.reset_for_tests()

    assert calls == []


def test_enabled_direct_application_composition_never_constructs_rehearsal(
    tmp_path,
    monkeypatch,
    _isolated_application_process,
) -> None:
    """An enabled rehearsal section remains inert in the public app root."""
    import sonder_runtime.bootstrap.app as bootstrap_app
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal
    from sonder_runtime.platform.config import load_config

    config_path, _state_home = _write_pooled_pair_toml(
        tmp_path,
        rehearsal_enabled=True,
    )
    _set_rehearsal_test_environment(monkeypatch)
    factory_calls: list[tuple[object, object]] = []
    provider_calls: list[tuple[object, object]] = []

    def factory_bomb(*args, **kwargs):
        factory_calls.append((args, kwargs))
        raise AssertionError("ordinary application composition must not build rehearsal")

    def provider_bomb(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("ordinary application composition must not create provider")

    monkeypatch.setattr(rehearsal, "build_control_state_rehearsal", factory_bomb)
    monkeypatch.setattr(HttpsControlStateProvider, "__init__", provider_bomb)
    config = load_config(config_path)

    application = bootstrap_app.build_application(config=config)
    try:
        assert application.config is config
    finally:
        application.close_providers(timeout=5)

    assert factory_calls == []
    assert provider_calls == []


@pytest.mark.parametrize(
    "case",
    ("serve", "repl-text", "repl-json", "mcp-legacy", "mcp-native"),
)
@pytest.mark.parametrize("rehearsal_enabled", (False, True))
def test_public_ordinary_entrypoints_never_construct_rehearsal(
    case: str,
    rehearsal_enabled: bool,
    tmp_path,
    monkeypatch,
    _isolated_application_process,
) -> None:
    """Use real parser/config/dispatch while replacing only terminal handoffs."""
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    config_path, state_home = _write_pooled_pair_toml(
        tmp_path,
        rehearsal_enabled=rehearsal_enabled,
    )
    _set_rehearsal_test_environment(monkeypatch)
    factory_calls: list[tuple[object, object]] = []
    provider_calls: list[tuple[object, object]] = []
    observed: list[tuple[str, object]] = []

    def factory_bomb(*args, **kwargs):
        factory_calls.append((args, kwargs))
        raise AssertionError("ordinary entrypoint must not build rehearsal")

    def provider_bomb(*args, **kwargs):
        provider_calls.append((args, kwargs))
        raise AssertionError("ordinary entrypoint must not create provider")

    monkeypatch.setattr(rehearsal, "build_control_state_rehearsal", factory_bomb)
    monkeypatch.setattr(HttpsControlStateProvider, "__init__", provider_bomb)
    _install_ordinary_entrypoint_stubs(monkeypatch, observed)

    assert entrypoint.main(_entrypoint_arguments(case, config_path)) == 0
    if case == "serve":
        composed = [value for name, value in observed if name == "serve-config"]
    elif case == "mcp-native":
        composed = [value for name, value in observed if name == "build-application"]
    else:
        composed = [value for name, value in observed if name == "default-app"]
    assert len(composed) == 1
    assert composed[0].control_state_rehearsal.enabled is rehearsal_enabled
    assert composed[0].state.home == str(state_home)
    assert factory_calls == []
    assert provider_calls == []
