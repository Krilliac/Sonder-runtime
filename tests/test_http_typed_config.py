from sonder_runtime.interfaces.http import serve
import pytest

from sonder_runtime.platform.config import (
    ObservabilityConfig,
    SonderConfig,
    ServerConfig,
    Secrets,
    apply_observability_environment,
    load_config,
    normalize_origin,
)


def test_http_boundary_binds_validated_typed_config(monkeypatch):
    names = (
        "API_KEY", "HOST", "REQUIRE_ACCOUNT", "AUTH_MODE", "CORS_ORIGINS",
        "OBSERVATORY_ORIGINS",
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
        observability=ObservabilityConfig(
            live_export_origins=("http://127.0.0.1:4173", "tauri://localhost"),
        ),
    )

    try:
        serve.configure_typed_config(config)

        assert serve.HOST == "127.0.0.1"
        assert serve.DEFAULT_PORT == 11435  # immutable module default
        assert serve.API_KEY == "k" * 32
        assert serve.CORS_ORIGINS == {"https://example.test"}
        # The Observatory grant is route-scoped and never joins the global list.
        assert serve.OBSERVATORY_ORIGINS == {"http://127.0.0.1:4173", "tauri://localhost"}
        assert serve.MAX_REQUEST_BYTES == 4096
        assert serve.REQUEST_TIMEOUT_SECONDS == 17
        assert serve.STREAM_IDLE_TIMEOUT_SECONDS == 9
        assert serve.HTTP_SESSION_STATE_LIMIT == 20
        assert serve.HTTP_SESSION_STATE_OWNER_LIMIT == 4
        assert serve.TRAIN_MAX_N == 33
    finally:
        for name, value in original.items():
            setattr(serve, name, value)


def test_observatory_export_environment_folds_into_typed_config():
    errors = []
    settings = apply_observability_environment(ObservabilityConfig(), {
        "SONDER_OBSERVATORY_EXPORT": "0",
        "SONDER_OBSERVATORY_BUFFER": "1000000",
        "SONDER_OBSERVATORY_MAX_SUBSCRIBERS": "3",
        "SONDER_OBSERVATORY_ORIGINS": "http://127.0.0.1:4173, http://localhost:4173",
    }, errors)
    assert errors == []
    assert settings.live_export is False
    assert settings.live_export_buffer == 65536
    assert settings.live_export_max_subscribers == 3
    assert settings.live_export_origins == ("http://127.0.0.1:4173", "http://localhost:4173")
    small = apply_observability_environment(
        ObservabilityConfig(), {"SONDER_OBSERVATORY_BUFFER": "12"}, errors,
    )
    assert small.live_export_buffer == 256
    defaults = ObservabilityConfig()
    assert (defaults.live_export, defaults.live_export_buffer,
            defaults.live_export_max_subscribers) == (True, 4096, 8)


def test_observatory_export_rejects_malformed_values(tmp_path):
    errors = []
    apply_observability_environment(
        ObservabilityConfig(), {"SONDER_OBSERVATORY_BUFFER": "lots"}, errors,
    )
    assert errors == ["SONDER_OBSERVATORY_BUFFER is not an integer"]
    config_path = tmp_path / "sonder.toml"
    config_path.write_text(
        "[observability]\nlive_export_max_subscribers = 0\n"
        "live_export_origins = [\"*\"]\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as caught:
        load_config(config_path, env={})
    message = str(caught.value)
    assert "live_export_max_subscribers" in message
    assert "live_export_origins" in message


def test_observatory_origins_are_normalised_with_a_warning_not_refused(caplog):
    """A trailing '/' or upper-case scheme/host is the same origin (logged)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="sonder.config"):
        config = load_config(env={
            "SONDER_OBSERVATORY_ORIGINS": "http://127.0.0.1:4173/, HTTP://LocalHost:4173",
        })
    assert config.observability.live_export_origins == (
        "http://127.0.0.1:4173", "http://localhost:4173",
    )
    assert sum("normalized" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:4173", "http://127.0.0.1:4173"),
    ("http://127.0.0.1:4173/", "http://127.0.0.1:4173"),
    ("HTTPS://Sonder.Example:443/", "https://sonder.example"),
    ("http://[::1]:5173/", "http://[::1]:5173"),
    ("tauri://localhost", "tauri://localhost"),
    # Not origins: left alone so validation still refuses them.
    ("http://127.0.0.1:4173/path", "http://127.0.0.1:4173/path"),
    ("*", "*"),
    ("http://host:port", "http://host:port"),
])
def test_normalize_origin(raw, expected):
    assert normalize_origin(raw) == expected


def test_a_path_is_still_not_an_origin():
    with pytest.raises(Exception) as caught:
        load_config(env={"SONDER_OBSERVATORY_ORIGINS": "http://127.0.0.1:4173/app"})
    assert "live_export_origins" in str(caught.value)
