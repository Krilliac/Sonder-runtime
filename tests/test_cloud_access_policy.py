import server
from sonder_runtime.domain.cloud_access import cloud_allowed, cloud_disabled_message


def test_cloud_allowed_accepts_only_explicit_true_values():
    assert cloud_allowed({"SONDER_ALLOW_CLOUD": "1"})
    assert cloud_allowed({"SONDER_ALLOW_CLOUD": " YES "})
    assert not cloud_allowed({})
    assert not cloud_allowed({"SONDER_ALLOW_CLOUD": "false"})


def test_cloud_disabled_message_is_pure_and_exact():
    expected = (
        "ERROR: hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )
    assert cloud_disabled_message() == expected


def test_server_compatibility_alias_preserves_function_identity():
    assert server._cloud_disabled_message is cloud_disabled_message
