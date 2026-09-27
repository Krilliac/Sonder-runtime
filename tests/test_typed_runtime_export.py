from __future__ import annotations

from types import SimpleNamespace

from sonder_runtime.__main__ import _export_runtime_environment
from sonder_runtime.platform.config import OllamaConfig, ServerConfig, SonderConfig


def test_canonical_export_leaves_typed_http_values_out_of_environment(monkeypatch):
    monkeypatch.setenv("SONDER_HOST", "poisoned-host")
    monkeypatch.setenv("SONDER_PORT", "9999")
    monkeypatch.setenv("SONDER_MAX_REQUEST_BYTES", "17")

    _export_runtime_environment(
        SonderConfig(server=ServerConfig(host="127.0.0.1", port=11435)),
        include_typed_runtime=False,
    )

    assert __import__("os").environ["SONDER_HOST"] == "poisoned-host"
    assert __import__("os").environ["SONDER_PORT"] == "9999"
    assert __import__("os").environ["SONDER_MAX_REQUEST_BYTES"] == "17"


def test_canonical_serve_export_preserves_toml_ollama_workers(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("SONDER_OLLAMA_WORKERS", "stale-worker")

    _export_runtime_environment(
        SonderConfig(
            ollama=OllamaConfig(
                url="http://127.0.0.1:11434",
                workers=("https://worker.example:11434",),
            )
        ),
        include_typed_runtime=False,
    )

    # Canonical serve binds the typed endpoint directly; it must not overwrite
    # the legacy environment override while still exporting the worker list.
    assert __import__("os").environ["OLLAMA_HOST"] == "http://127.0.0.1:1"
    assert __import__("os").environ["SONDER_OLLAMA_WORKERS"] == (
        "https://worker.example:11434"
    )


def test_legacy_export_includes_typed_ollama_static_roster_bounds():
    environment = __import__("os").environ
    before = dict(environment)
    try:
        _export_runtime_environment(
            SonderConfig(
                ollama=OllamaConfig(
                    worker_pool_max_workers=64,
                    worker_capability_probe_parallelism=8,
                    worker_capability_probe_batch_size=128,
                    worker_status_page_size=128,
                )
            )
        )
        assert environment["SONDER_OLLAMA_POOL_MAX_WORKERS"] == "64"
        assert environment["SONDER_OLLAMA_WORKER_PROBE_PARALLELISM"] == "8"
        assert environment["SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE"] == "128"
        assert environment["SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE"] == "128"
    finally:
        environment.clear()
        environment.update(before)


def _typed_capacity_config() -> SonderConfig:
    return SonderConfig(
        ollama=OllamaConfig(
            worker_pool_max_workers=64,
            worker_max_inflight=7,
            worker_queue_depth=90,
            worker_capability_probe_parallelism=8,
            worker_capability_probe_batch_size=128,
            worker_status_page_size=128,
        )
    )


def _capture_typed_worker_configuration(monkeypatch, *, apply=False):
    from sonder_runtime.adapters.inference import ollama_pool

    captured = []
    configure = ollama_pool.configure_typed_workers

    def capture(worker_origins, **options):
        captured.append((worker_origins, options))
        if apply:
            configure(worker_origins, **options)

    monkeypatch.setattr(ollama_pool, "configure_typed_workers", capture)
    return captured


def _assert_typed_capacity_was_forwarded(captured) -> None:
    assert len(captured) == 1
    worker_origins, options = captured[0]
    assert worker_origins == ()
    assert options.get("max_workers") == 64
    assert options.get("max_inflight_per_worker") == 7
    assert options.get("queue_depth") == 90
    assert options.get("capability_probe_parallelism") == 8
    assert options.get("capability_probe_batch_size") == 128
    assert options.get("status_page_size") == 128


def test_cmd_serve_forwards_typed_ollama_capacity_to_worker_pool(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters import embeddings
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration
    from sonder_runtime.domain.common.errors import MigrationRequired

    config = _typed_capacity_config()
    captured = _capture_typed_worker_configuration(monkeypatch)
    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: config)
    monkeypatch.setattr(
        ollama_endpoint, "configure_typed_endpoint", lambda _origin: None
    )
    monkeypatch.setattr(
        embeddings, "configure_typed_endpoint", lambda _origin: None
    )

    def stop_before_migration(_home):
        raise MigrationRequired("test stop")

    monkeypatch.setattr(
        bridge_migration, "require_epoch_2", stop_before_migration
    )

    assert entrypoint.cmd_serve(SimpleNamespace(skip_preflight=True)) == 1

    _assert_typed_capacity_was_forwarded(captured)


def test_legacy_mcp_forwards_typed_ollama_capacity_to_worker_pool(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import legacy_mcp

    config = _typed_capacity_config()
    captured = _capture_typed_worker_configuration(monkeypatch)
    events = []
    closed = []

    class Runtime:
        def require_startup_safety(self):
            events.append("safety")

        def run(self, *, safety_checked):
            events.append(("run", safety_checked))

    class Application:
        def close_providers(self, *, timeout):
            raise AssertionError("legacy MCP must use detached default cleanup")

    application = Application()
    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: config)
    monkeypatch.setattr(
        entrypoint, "_export_runtime_environment", lambda _config: None
    )
    monkeypatch.setattr(
        entrypoint, "build_legacy_server_mcp_runtime", lambda: Runtime()
    )
    monkeypatch.setattr(
        ollama_endpoint, "configure_typed_endpoint", lambda _origin: None
    )
    monkeypatch.setattr(
        bootstrap_app, "default_app", lambda *, config: application
    )
    monkeypatch.setattr(
        bootstrap_app,
        "close_default_runtime_resources",
        lambda *, timeout: closed.append(timeout),
    )
    monkeypatch.setattr(
        legacy_mcp, "configure_legacy_application", lambda _app: None
    )

    assert entrypoint.cmd_mcp(SimpleNamespace(native=False)) == 0

    _assert_typed_capacity_was_forwarded(captured)
    assert events == ["safety", ("run", True)]
    assert closed == [5]


def test_cmd_repl_uses_the_detached_default_graph_cleanup_once(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.persistence import migrations
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import legacy_interfaces
    from sonder_runtime.interfaces.repl import repl

    config = SonderConfig()
    calls = []

    class Application:
        def close_providers(self, *, timeout):
            raise AssertionError("command must close the detached default owner")

    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: config)
    monkeypatch.setattr(entrypoint, "_configure_typed_home", lambda _config: None)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda _config: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_kwargs: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_interfaces", lambda: None)
    monkeypatch.setattr(legacy_interfaces, "configure_legacy_application", lambda _app: None)
    monkeypatch.setattr(bootstrap_app, "default_app", lambda *, config: Application())
    monkeypatch.setattr(
        bootstrap_app,
        "close_default_runtime_resources",
        lambda *, timeout: calls.append(timeout),
    )
    monkeypatch.setattr(repl, "run_jsonl", lambda: calls.append("repl"))

    assert entrypoint.cmd_repl(SimpleNamespace(json=True)) == 0
    assert calls == ["repl", 5]


def test_native_mcp_entrypoint_owns_the_full_graph_close(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.persistence import migrations
    from sonder_runtime.adapters.security import unsafe_lab
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.bootstrap import native_mcp

    config = SonderConfig()
    calls = []
    application = SimpleNamespace(
        close_providers=lambda *, timeout: calls.append(("close", timeout))
    )

    monkeypatch.setattr(unsafe_lab, "require_startup", lambda: None)
    monkeypatch.setattr(entrypoint, "_load_config", lambda _args: config)
    monkeypatch.setattr(entrypoint, "_configure_typed_home", lambda _config: None)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_kwargs: None)
    monkeypatch.setattr(bootstrap_app, "build_application", lambda *, config: application)
    monkeypatch.setattr(
        native_mcp,
        "run_native_mcp",
        lambda app, *, close_compute_on_exit: calls.append(
            ("run", app, close_compute_on_exit)
        ) or 17,
    )

    # The frame count run_native_mcp returns is not an exit status.
    assert entrypoint.cmd_mcp(SimpleNamespace(native=True)) == 0
    assert calls == [("run", application, False), ("close", 5)]


def test_build_application_forwards_typed_ollama_capacity_to_worker_pool(
    monkeypatch,
):
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.bootstrap import app as bootstrap_app

    config = _typed_capacity_config()
    captured = _capture_typed_worker_configuration(monkeypatch, apply=True)
    application = None
    try:
        application = bootstrap_app.build_application(config=config)
        assert application.config is config
        assert application.inference_pool.summary()["configured_worker_limit"] == 64
    finally:
        if application is not None:
            application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()
        lifecycle.reset_for_tests()

    _assert_typed_capacity_was_forwarded(captured)
