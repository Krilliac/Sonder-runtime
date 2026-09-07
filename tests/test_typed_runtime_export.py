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


def _typed_capacity_config() -> SonderConfig:
    return SonderConfig(
        ollama=OllamaConfig(
            worker_max_inflight=7,
            worker_queue_depth=90,
        )
    )


def _capture_typed_worker_configuration(monkeypatch):
    from sonder_runtime.adapters.inference import ollama_pool

    captured = []

    def capture(worker_origins, **options):
        captured.append((worker_origins, options))

    monkeypatch.setattr(ollama_pool, "configure_typed_workers", capture)
    return captured


def _assert_typed_capacity_was_forwarded(captured) -> None:
    assert len(captured) == 1
    worker_origins, options = captured[0]
    assert worker_origins == ()
    assert options.get("max_inflight_per_worker") == 7
    assert options.get("queue_depth") == 90


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
            closed.append(timeout)

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
        legacy_mcp, "configure_legacy_application", lambda _app: None
    )

    assert entrypoint.cmd_mcp(SimpleNamespace(native=False)) == 0

    _assert_typed_capacity_was_forwarded(captured)
    assert events == ["safety", ("run", True)]
    assert closed == [5]


def test_build_application_forwards_typed_ollama_capacity_to_worker_pool(
    monkeypatch,
):
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.bootstrap import app as bootstrap_app

    config = _typed_capacity_config()
    captured = _capture_typed_worker_configuration(monkeypatch)
    monkeypatch.setattr(
        ollama_endpoint, "configure_typed_endpoint", lambda _origin: None
    )

    try:
        application = bootstrap_app.build_application(config=config)
        assert application.config is config
    finally:
        lifecycle.reset_for_tests()

    _assert_typed_capacity_was_forwarded(captured)
