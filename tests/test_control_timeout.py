import server

from sonder_runtime.domain.control_timeout import parse_control_timeout


def test_server_keeps_identity_compatible_timeout_policy_alias():
    assert server._parse_control_timeout is parse_control_timeout


def test_parse_control_timeout_defaults_and_clamps_to_runtime_bounds():
    assert parse_control_timeout("") == (8, None)
    assert parse_control_timeout(" 0 ") == (1, None)
    assert parse_control_timeout("90") == (60, None)
    assert parse_control_timeout("17") == (17, None)


def test_parse_control_timeout_reports_command_specific_usage():
    assert parse_control_timeout("not-a-number", "/runproject") == (
        None,
        "usage: /runproject [seconds]  (runs the previous fenced code block)",
    )
