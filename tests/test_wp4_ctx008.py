import pytest

from sonder_runtime.domain.context.hardware_sizing import (
    MeasuredContextCapability,
    native_context_size,
    size_native_context,
)


def capability(**overrides):
    values = {
        "measured_context_tokens": 16_000,
        "measured_free_memory_gb": 8.0,
        "available_memory_gb": 8.0,
        "model_id": "model:q4",
        "kv_cache_type": "q8_0",
    }
    values.update(overrides)
    return MeasuredContextCapability(**values)


def test_measured_capability_is_scaled_down_by_both_margins():
    result = size_native_context(
        capability(available_memory_gb=10.0),
        memory_safety_margin=0.8,
        token_safety_margin=0.9,
    )

    assert result.context_tokens == 14_400
    assert result.raw_context_tokens == 14_400
    assert result.source == "measured"
    assert result.reason == "scaled_measured_capability"


def test_sizing_rounds_down_and_respects_bounds():
    result = size_native_context(
        capability(measured_context_tokens=1_001),
        memory_safety_margin=0.8,
        token_safety_margin=0.9,
        minimum_context_tokens=512,
        maximum_context_tokens=900,
    )
    assert result.context_tokens == 720
    assert result.raw_context_tokens == 720

    capped = size_native_context(
        capability(measured_context_tokens=100_000, available_memory_gb=100.0),
        maximum_context_tokens=20_000,
    )
    assert capped.context_tokens == 20_000


@pytest.mark.parametrize(
    "bad",
    [None, "not-a-capability", capability(measured_context_tokens=0),
     capability(measured_free_memory_gb=0), capability(available_memory_gb=0)],
)
def test_invalid_or_missing_measurements_use_same_deterministic_fallback(bad):
    first = native_context_size(bad)
    second = native_context_size(bad)

    assert first == second
    assert first.context_tokens == 8_192
    assert first.source == "fallback"
    assert first.reason == "invalid_or_unavailable_measurement"
    assert first.raw_context_tokens is None


def test_fallback_is_bounded_by_explicit_policy_limits():
    result = size_native_context(
        None, fallback_context_tokens=10_000, minimum_context_tokens=512,
        maximum_context_tokens=9_000,
    )
    assert result.context_tokens == 9_000


@pytest.mark.parametrize("name", ["memory_safety_margin", "token_safety_margin"])
def test_margins_must_be_finite_positive_fractions(name):
    kwargs = {name: 0}
    with pytest.raises(ValueError, match="between 0 and 1"):
        size_native_context(capability(), **kwargs)
