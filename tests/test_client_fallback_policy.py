import sonder_client
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
