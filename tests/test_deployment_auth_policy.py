from sonder_runtime.platform.deployment_auth import authenticates_callers


def test_local_open_environment_does_not_authenticate_callers():
    assert not authenticates_callers(environ={})


def test_any_auth_mode_makes_callers_distinguishable():
    assert authenticates_callers(environ={"SONDER_AUTH_MODE": "account"})


def test_api_key_makes_callers_distinguishable():
    assert authenticates_callers(environ={"SONDER_API_KEY": "configured"})


def test_required_account_accepts_historical_boolean_values():
    for value in ("1", "true", "YES", "on"):
        assert authenticates_callers(environ={"SONDER_REQUIRE_ACCOUNT": value})


def test_unrecognized_account_value_does_not_authenticate():
    assert not authenticates_callers(environ={"SONDER_REQUIRE_ACCOUNT": "maybe"})


def test_blank_settings_remain_local_open():
    assert not authenticates_callers(
        environ={
            "SONDER_AUTH_MODE": "  ",
            "SONDER_API_KEY": "\t",
            "SONDER_REQUIRE_ACCOUNT": "0",
        }
    )
