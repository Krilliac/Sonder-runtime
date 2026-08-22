import sonder_client
from sonder_runtime.adapters import client_endpoint
from sonder_runtime.platform import client_fallback


def test_fallback_policy_defaults_to_enabled():
    assert client_fallback.enabled(environ={}) is True


def test_fallback_policy_accepts_disabled_values():
    for value in ("0", "false", "no", "off", " FALSE "):
        assert client_fallback.enabled(environ={"SONDER_FALLBACK_LOCAL": value}) is False


def test_fallback_policy_accepts_other_values():
    for value in ("1", "true", "yes", "on", "unexpected"):
        assert client_fallback.enabled(environ={"SONDER_FALLBACK_LOCAL": value}) is True


def test_client_keeps_compatibility_alias():
    assert sonder_client.local_fallback_enabled is client_fallback.enabled


def test_client_endpoint_adapter_owns_server_comparison():
    assert sonder_client._same_server is client_endpoint.same_server
    assert client_endpoint.same_server(" http://host/// ", "http://host/") is True
    assert client_endpoint.same_server("http://host", "http://other") is False


def test_client_endpoint_adapter_resolves_local_fallback_from_environment():
    assert client_endpoint.local_fallback_server(environ={}) == "http://127.0.0.1:11435"
    assert client_endpoint.local_fallback_server(
        environ={"SONDER_LOCAL_FALLBACK": "http://local:9"}
    ) == "http://local:9"
