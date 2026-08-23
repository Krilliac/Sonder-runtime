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


def test_auto_context_scales_with_model_size_and_advertised_limit(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")

    assert context_policy.auto_context(262144, "30.5B") == 16384
    assert context_policy.auto_context(40960, "14.8B") == 24576
    assert context_policy.auto_context(32768, "7.6B") == 32768
    assert context_policy.auto_context(8192, "7.6B") == 8192

    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "6k")
    assert context_policy.auto_context(262144, "30.5B") == 6000


def test_auto_context_adds_a_70b_class_band(monkeypatch):
    """A 70B-class model spends far more KV bytes per token than a 30B; the
    auto window must shrink again instead of sharing the 24B band."""
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")

    assert context_policy.auto_context(131072, "70.6B") == 8192
    assert context_policy.auto_context(131072, "72B") == 8192
    # The band below is unchanged.
    assert context_policy.auto_context(131072, "32B") == 16384


def test_auto_context_ignores_unknown_or_malformed_metadata(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")

    assert context_policy.auto_context(None, None) == 32768
    assert context_policy.auto_context("garbage", "unknown") == 32768
    # Sub-billion models never hit a band.
    assert context_policy.auto_context(None, "780M") == 32768


def test_explicit_operator_context_overrides_the_parameter_band(monkeypatch):
    """SONDER_CONTEXT_SIZE is an informed override of the KV budget ladder,
    matching the documented contract; the model's advertised maximum and the
    native ceiling remain physical limits."""
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "32k")

    assert context_policy.auto_context(262144, "30.5B") == 32000
    assert context_policy.auto_context(16384, "30.5B") == 16384

    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "24k")
    assert context_policy.auto_context(262144, "30.5B") == 24000


def test_auto_context_plan_records_clamp_provenance(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")

    plan = context_policy.auto_context_plan(262144, "30.5B")
    assert plan["context"] == 16384
    assert plan["base"] == 32768
    assert plan["source"] == "kv-quantised-default"
    assert plan["parameter_billions"] == 30.5
    assert plan["advertised"] == 262144
    assert plan["clamps"] == ("parameters>=24B",)

    plan = context_policy.auto_context_plan(8192, "7.6B")
    assert plan["context"] == 8192
    assert plan["clamps"] == ("advertised-maximum",)

    plan = context_policy.auto_context_plan(100, None)
    assert plan["context"] == context_policy.MIN_CONTEXT
    assert plan["clamps"] == ("advertised-maximum", "minimum-window")

    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "6k")
    plan = context_policy.auto_context_plan(262144, "30.5B")
    assert plan["context"] == 6000
    assert plan["source"] == "environment"
    assert plan["clamps"] == ()
