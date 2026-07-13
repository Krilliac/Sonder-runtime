import context_policy


def test_parse_size_accepts_suffixes():
    assert context_policy.parse_size("32k") == 32000
    assert context_policy.parse_size("1m") == 1000000
    assert context_policy.parse_size("262,144") == 262144


def test_policy_clamps_native_but_allows_virtual(monkeypatch):
    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "256k")
    monkeypatch.setenv("SONDER_VIRTUAL_CONTEXT_MAX", "1m")

    policy = context_policy.policy("1m")

    assert policy["requested"] == 1000000
    assert policy["native"] == 256000
    assert policy["virtual"] is True
    assert policy["mode"] == "virtual"


def test_requested_clamps_to_virtual_max(monkeypatch):
    monkeypatch.setenv("SONDER_VIRTUAL_CONTEXT_MAX", "500k")

    assert context_policy.requested("1m") == 500000


def test_parse_strict_rejects_invalid_and_degenerate_sizes():
    import context_policy as cp
    # valid
    assert cp.parse_strict("8192") == 8192
    assert cp.parse_strict("32k") == 32000
    assert cp.parse_strict("1m") == 1000000
    assert cp.parse_strict(4096) == 4096
    # invalid / degenerate -> None (so set_context_size can reject, not default)
    assert cp.parse_strict("0") is None
    assert cp.parse_strict("-5") is None
    assert cp.parse_strict("abc") is None
    assert cp.parse_strict("") is None
    assert cp.parse_strict(None) is None
    assert cp.parse_strict(True) is None
