import server

from sonder_runtime.domain.campaign_expectations import campaign_expected


def test_server_keeps_identity_compatible_campaign_expected_alias():
    assert server._campaign_expected is campaign_expected


def test_campaign_expected_preserves_all_task_verdicts():
    assert campaign_expected("hello") == "sonder-ok"
    assert campaign_expected("sum") == "42"
    assert campaign_expected("loop") == "1\n2\n3"
    assert campaign_expected("string") == "rednos"
    assert campaign_expected("branch") == "prime"
    assert campaign_expected("list") == "20"
    assert campaign_expected("toposort") == "d a b c"
    assert campaign_expected("lru") == "10 -1 30"
    assert campaign_expected("intervals") == "1-6 8-12"
    assert campaign_expected("balanced") == "ok\nbad\nbad"
    assert campaign_expected("wordfreq") == "the:3"
    assert campaign_expected("fib") == "6765"


def test_campaign_expected_returns_empty_for_unknown_task():
    assert campaign_expected("unknown") == ""
    assert campaign_expected(None) == ""
