import server
from sonder_runtime.domain.campaign_formatting import campaign_headline


def test_server_keeps_identity_compatible_campaign_headline_alias():
    assert server._campaign_headline is campaign_headline


def test_campaign_headline_keeps_healthy_output_byte_identical():
    assert campaign_headline(24, 24, 24, 0, 0, 12.5) == (
        "campaign generate/compile/execute/record: "
        "24/24 passed, 24 recorded, 0 failed-recorded in 12.500s"
    )


def test_campaign_headline_surfaces_nonzero_pitfall_errors():
    assert "3 pitfall-errors" in campaign_headline(20, 24, 20, 4, 3, 12.5)
