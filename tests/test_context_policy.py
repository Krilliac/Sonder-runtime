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


def test_default_context_follows_the_kv_cache_type(monkeypatch):
    """How much context fits on a small card depends almost entirely on how
    the KV cache is stored. Measured on a 6 GiB RTX 4050 with a 7B Q4_K_M
    resident: fp16 KV held 8192 at 5301 MiB and could not fit 32768, while
    q8_0 KV held 32768 at 5383 MiB. The default reads the serving config
    instead of assuming one, and an absent variable yields the smaller,
    always-safe window."""
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)

    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
    assert context_policy.default_context() == 8192
    assert context_policy.native() == 8192

    for quantised in ("q8_0", "q4_0", "Q8_0", " q5_1 "):
        monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", quantised)
        assert context_policy.default_context() == 32768, quantised

    # An unrecognised value is treated as unquantised rather than optimistic.
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "f16")
    assert context_policy.default_context() == 8192

    # An explicit request always wins over the inferred default.
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "4096")
    assert context_policy.native() == 4096
