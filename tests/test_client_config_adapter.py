import sonder_client
from sonder_runtime.adapters import client_config


def test_client_config_adapter_owns_argv_parser():
    assert sonder_client._parse_argv is client_config.parse_argv
    assert client_config.parse_argv(["--server", "http://host", "--key", "secret"]) == (
        "http://host",
        "secret",
    )


def test_client_config_adapter_resolves_environment_defaults():
    environ = {"SONDER_SERVER": "http://env", "SONDER_API_KEY": "env-key"}
    assert client_config.resolve_config([], environ=environ) == (
        "http://env",
        "env-key",
    )


def test_client_config_adapter_gives_argv_precedence():
    environ = {"SONDER_SERVER": "http://env", "SONDER_API_KEY": "env-key"}
    assert client_config.resolve_config(
        ["--server", "http://argv", "--key", "argv-key"],
        environ=environ,
    ) == ("http://argv", "argv-key")


def test_client_config_adapter_ignores_incomplete_or_unknown_arguments():
    assert client_config.parse_argv(["--unknown", "value", "--server"]) == (None, None)


def test_root_resolve_config_delegates_to_adapter(monkeypatch):
    monkeypatch.setenv("SONDER_SERVER", "http://env")
    monkeypatch.setenv("SONDER_API_KEY", "env-key")
    assert sonder_client.resolve_config(["--server", "http://argv"]) == (
        "http://argv",
        "env-key",
    )
