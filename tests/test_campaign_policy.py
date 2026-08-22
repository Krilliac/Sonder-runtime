import server
from sonder_runtime.domain.campaign_policy import output_matches


def test_server_keeps_identity_compatible_campaign_match_alias():
    assert server._campaign_output_matches is output_matches


def test_output_matches_normalizes_only_outer_and_line_whitespace():
    assert output_matches(" 1\r\n2 \r\n", "1\n2")
    assert output_matches("\nsonder-ok\n", "sonder-ok")
    assert output_matches("", "")


def test_output_matches_rejects_prose_and_extra_lines():
    assert not output_matches("The answer is 42.", "42")
    assert not output_matches("0\n1\n2", "1\n2")
    assert not output_matches("valid\ninvalid", "ok\nbad")
