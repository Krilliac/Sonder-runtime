"""Ownership tests for the private-COT environment policy."""

from sonder_runtime.platform.private_cot_policy import opt_in_enabled


def test_private_cot_policy_is_off_when_flag_is_absent():
    assert opt_in_enabled(environ={}) is False


def test_private_cot_policy_accepts_documented_truthy_values():
    for value in ("1", "true", "yes", "on", "TRUE"):
        assert opt_in_enabled(environ={"SONDER_ALLOW_PRIVATE_COT": value}) is True


def test_private_cot_policy_rejects_falsy_and_unknown_values():
    for value in ("", "0", "false", "no", "off", "maybe", "  "):
        assert opt_in_enabled(environ={"SONDER_ALLOW_PRIVATE_COT": value}) is False


def test_server_compatibility_wrapper_uses_packaged_owner(monkeypatch):
    import server

    monkeypatch.setattr(server, "_private_cot_opt_in_enabled_policy", lambda: True)
    assert server.private_cot_opt_in_enabled() is True
