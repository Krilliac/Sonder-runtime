"""Focused ARCH-009 tests for the typed HTTP configuration boundary."""

from types import SimpleNamespace

import pytest

from sonder_runtime.interfaces.http import serve
from sonder_runtime.platform.config import Secrets, ServerConfig, SonderConfig


@pytest.fixture(autouse=True)
def restore_http_globals():
    names = (
        "CONFIGURED_PORT", "API_KEY", "AUTH_SECRET", "HOST", "REQUIRE_ACCOUNT",
        "AUTH_MODE", "CORS_ORIGINS", "TLS_TERMINATED_BY_PROXY",
        "ALLOW_REGISTRATION", "MAX_REQUEST_BYTES", "MAX_DISCARDED_BODY_BYTES",
        "REQUEST_TIMEOUT_SECONDS", "STREAM_IDLE_TIMEOUT_SECONDS",
        "HTTP_SESSION_STATE_LIMIT", "HTTP_SESSION_STATE_OWNER_LIMIT", "TRAIN_MAX_N",
    )
    original = {name: getattr(serve, name) for name in names}
    yield
    for name, value in original.items():
        setattr(serve, name, value)


def _config(*, port=12001, auth_secret="typed-auth-secret-" + ("s" * 32)):
    return SonderConfig(
        server=ServerConfig(
            port=port,
            host="127.0.0.1",
            auth_mode="both",
            request_timeout_seconds=17,
            stream_idle_timeout_seconds=9,
            max_request_bytes=4096,
            cors_origins=("https://typed.example",),
            require_account=True,
            allow_registration=True,
        ),
        secrets=Secrets(api_key="typed-api-key-" + ("k" * 24), auth_secret=auth_secret),
    )


def test_typed_http_values_are_not_replaced_by_environment(monkeypatch):
    monkeypatch.setenv("SONDER_PORT", "19999")
    monkeypatch.setenv("SONDER_AUTH_SECRET", "poisoned-environment-secret")
    monkeypatch.setenv("SONDER_HOST", "0.0.0.0")
    monkeypatch.setenv("SONDER_MAX_REQUEST_BYTES", "1")

    serve.configure_typed_config(_config())

    assert serve.CONFIGURED_PORT == 12001
    assert serve.AUTH_SECRET == "typed-auth-secret-" + ("s" * 32)
    assert serve.HOST == "127.0.0.1"
    assert serve.MAX_REQUEST_BYTES == 4096
    assert serve.CORS_ORIGINS == {"https://typed.example"}


def test_typed_main_uses_configured_port_when_environment_is_poisoned(monkeypatch):
    monkeypatch.setenv("SONDER_PORT", "19999")
    config = _config(port=12345)
    bound = {}

    class FakeServer:
        def __init__(self, address, handler):
            bound["address"] = address

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    lifecycle = SimpleNamespace(
        startup=lambda: None,
        begin_ollama_probe=lambda: None,
        coordinator=SimpleNamespace(
            add_flush_hook=lambda hook: None,
            draining=False,
        ),
        drain=lambda reason: None,
    )
    monkeypatch.setattr(serve, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(serve.sonder_lifecycle, "get", lambda: lifecycle)
    monkeypatch.setattr(serve.server, "runtime_source_update_status", lambda refresh=False: "ok")

    serve.main(config)

    assert bound["address"] == ("127.0.0.1", 12345)


def test_typed_auth_secret_is_used_for_non_loopback_validation(monkeypatch):
    monkeypatch.setenv("SONDER_AUTH_SECRET", "poisoned-environment-secret")
    typed_secret = "typed-auth-secret-" + ("s" * 32)
    serve.configure_typed_config(_config(auth_secret=typed_secret))

    # Explicitly passing the typed host/auth values exercises the same policy
    # used immediately before a configured listener binds.
    serve._validate_bind_security(
        "192.0.2.10",
        api_key="typed-api-key-" + ("k" * 24),
        auth_mode="both",
        tls_terminated_by_proxy=True,
    )
