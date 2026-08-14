"""SPEC-2 WP2: typed, deterministic, fail-closed configuration."""
from __future__ import annotations

import os

import pytest

import sonder_config
from sonder_config import ConfigError, load_config

pytestmark = pytest.mark.unit

_CLEAN_ENV: dict[str, str] = {}


def _strong_key() -> str:
    return "k" * sonder_config.MIN_API_KEY_LENGTH


def test_defaults_are_loopback_and_closed():
    config = load_config(env=_CLEAN_ENV)
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 11435
    assert config.profile == "workstation-local"
    assert config.features.cloud is False
    assert config.features.web is False
    assert config.ollama.allow_remote is False
    assert config.server.tls_terminated_by_proxy is False


def test_toml_profile_loads(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
schema_version = 1
profile = "workstation-local"

[server]
port = 12345

[capacity]
queue_depth = 8
""",
        encoding="utf-8",
    )
    config = load_config(toml, env=_CLEAN_ENV)
    assert config.server.port == 12345
    assert config.capacity.queue_depth == 8
    assert str(toml) in config.sources


def test_non_loopback_without_tls_proxy_fails(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert any("tls_terminated_by_proxy" in e for e in excinfo.value.errors)


def test_non_loopback_without_strong_key_fails(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
tls_terminated_by_proxy = true
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env={"SONDER_API_KEY": "short"})
    assert any("SONDER_API_KEY" in e for e in excinfo.value.errors)


def test_non_loopback_with_proxy_and_strong_key_passes(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
host = "0.0.0.0"
tls_terminated_by_proxy = true
""",
        encoding="utf-8",
    )
    config = load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert config.server.host == "0.0.0.0"


def test_server_private_profile_requires_strong_key(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text('profile = "server-private"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    assert any("server-private" in e for e in excinfo.value.errors)
    config = load_config(toml, env={"SONDER_API_KEY": _strong_key()})
    assert config.profile == "server-private"


def test_secrets_in_toml_rejected(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
api_key = "super-secret-value-here"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    assert any("secrets environment file" in e for e in excinfo.value.errors)


def test_all_errors_reported_together(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
schema_version = 99
profile = "public-saas"

[server]
port = 999999
auth_mode = "none"

[observability]
log_level = "SHOUT"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    errors = "\n".join(excinfo.value.errors)
    assert "schema_version" in errors
    assert "profile" in errors
    assert "port" in errors
    assert "auth_mode" in errors
    assert "log_level" in errors
    assert len(excinfo.value.errors) >= 5


def test_unknown_keys_rejected(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text(
        """
[server]
prot = 1234

[surver]
port = 1234
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(toml, env=_CLEAN_ENV)
    errors = "\n".join(excinfo.value.errors)
    assert "[server].prot" in errors
    assert "surver" in errors


def test_env_compatibility_and_precedence(tmp_path):
    toml = tmp_path / "sonder.toml"
    toml.write_text("[server]\nport = 12000\n", encoding="utf-8")
    config = load_config(
        toml,
        env={"SONDER_PORT": "13000", "SONDER_ALLOW_CLOUD": "1"},
    )
    assert config.server.port == 13000  # env beats TOML
    assert config.features.cloud is True
    config = load_config(
        toml,
        env={"SONDER_PORT": "13000"},
        overrides={"server.port": "14000"},
    )
    assert config.server.port == 14000  # CLI beats env


def test_secrets_file_loaded_and_permission_checked(tmp_path):
    secrets = tmp_path / "sonder.env"
    secrets.write_text(f"SONDER_API_KEY={_strong_key()}\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    config = load_config(secrets_path=secrets, env=_CLEAN_ENV)
    assert config.secrets.api_key == _strong_key()

    if os.name == "posix":
        os.chmod(secrets, 0o644)
        with pytest.raises(ConfigError) as excinfo:
            load_config(secrets_path=secrets, env=_CLEAN_ENV)
        assert any("group/world" in e for e in excinfo.value.errors)


def test_process_env_beats_secrets_file(tmp_path):
    secrets = tmp_path / "sonder.env"
    secrets.write_text("SONDER_API_KEY=" + "a" * 32 + "\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    config = load_config(
        secrets_path=secrets, env={"SONDER_API_KEY": "b" * 32}
    )
    assert config.secrets.api_key == "b" * 32


def test_remote_ollama_requires_consent():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"OLLAMA_HOST": "192.168.1.50:11434"})
    assert any("remote-Ollama consent" in e for e in excinfo.value.errors)
    config = load_config(
        env={
            "OLLAMA_HOST": "https://192.168.1.50:11434",
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        }
    )
    assert config.ollama.allow_remote is True


def test_remote_ollama_requires_https_even_with_consent():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            env={
                "OLLAMA_HOST": "http://192.168.1.50:11434",
                "SONDER_ALLOW_REMOTE_OLLAMA": "1",
            }
        )
    assert any("must use https" in error for error in excinfo.value.errors)


def test_redacted_dump_never_contains_secret_values():
    secret = "extremely-secret-api-key-value-123"
    config = load_config(env={"SONDER_API_KEY": secret,
                              "SONDER_AUTH_SECRET": secret + "auth"})
    dumped = repr(config.as_redacted_dict())
    assert secret not in dumped
    assert config.as_redacted_dict()["secrets"]["api_key"] == "[set]"
