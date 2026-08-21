from sonder_runtime.interfaces.http import serve
from sonder_runtime.platform.config import (
    SonderConfig,
    ServerConfig,
    Secrets,
)


def test_http_boundary_binds_validated_typed_config(monkeypatch):
    names = (
        "API_KEY", "HOST", "REQUIRE_ACCOUNT", "AUTH_MODE", "CORS_ORIGINS",
        "TLS_TERMINATED_BY_PROXY", "ALLOW_REGISTRATION", "MAX_REQUEST_BYTES",
        "MAX_DISCARDED_BODY_BYTES", "REQUEST_TIMEOUT_SECONDS",
        "STREAM_IDLE_TIMEOUT_SECONDS", "HTTP_SESSION_STATE_LIMIT",
        "HTTP_SESSION_STATE_OWNER_LIMIT", "TRAIN_MAX_N",
    )
    original = {name: getattr(serve, name) for name in names}
    config = SonderConfig(
        server=ServerConfig(
            host="127.0.0.1",
            port=12001,
            auth_mode="api-key",
            max_request_bytes=4096,
            request_timeout_seconds=17,
            stream_idle_timeout_seconds=9,
            cors_origins=("https://example.test",),
            require_account=False,
            allow_registration=True,
            session_state_limit=20,
            session_state_owner_limit=4,
            train_max_n=33,
        ),
        secrets=Secrets(api_key="k" * 32),
    )

    try:
        serve.configure_typed_config(config)

        assert serve.HOST == "127.0.0.1"
        assert serve.DEFAULT_PORT == 11435  # immutable module default
        assert serve.API_KEY == "k" * 32
        assert serve.CORS_ORIGINS == {"https://example.test"}
        assert serve.MAX_REQUEST_BYTES == 4096
        assert serve.REQUEST_TIMEOUT_SECONDS == 17
        assert serve.STREAM_IDLE_TIMEOUT_SECONDS == 9
        assert serve.HTTP_SESSION_STATE_LIMIT == 20
        assert serve.HTTP_SESSION_STATE_OWNER_LIMIT == 4
        assert serve.TRAIN_MAX_N == 33
    finally:
        for name, value in original.items():
            setattr(serve, name, value)
