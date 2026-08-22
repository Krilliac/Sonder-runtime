from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.routing.calibration import CalibrationRegistry
from sonder_runtime.domain.routing.calibration_profiles import CalibrationProfile


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def profile(model, *, throughput=20, latency=100, resident=8, age=0, samples=3):
    return CalibrationProfile(
        model=model, hardware="rtx-5070-ti", quantization="Q4_K_M",
        resident_gb=resident, throughput_tokens_per_second=throughput,
        latency_ms_p95=latency, measured_at=NOW - timedelta(seconds=age),
        sample_count=samples,
    )


def test_profile_rejects_invalid_measurements_and_normalizes_time():
    with pytest.raises(ValueError):
        profile("bad", throughput=0)
    observed = profile("qwen:30b", age=10)
    assert observed.measured_at.tzinfo is not None
    assert len(observed.digest()) == 64


def test_registry_replaces_stale_observation_only_with_newer_one():
    registry = CalibrationRegistry([profile("a", throughput=10, age=20)])
    old = registry.record(profile("a", throughput=99, age=30))
    assert old.throughput_tokens_per_second == 10
    fresh = registry.record(profile("a", throughput=12, age=1))
    assert fresh.throughput_tokens_per_second == 12


def test_selection_uses_measured_fit_and_is_deterministic():
    registry = CalibrationRegistry([
        profile("slow", throughput=10, resident=4),
        profile("fast", throughput=30, resident=12),
        profile("too-large", throughput=99, resident=20),
    ])
    selected = registry.select(
        hardware="RTX-5070-TI", models=["slow", "fast", "too-large"],
        now=NOW, max_resident_gb=12,
    )
    assert selected is not None and selected.profile.model == "fast"
    assert registry.select(hardware="other", models=["fast"], now=NOW) is None


def test_stale_profiles_are_not_used_as_measured_routing_facts():
    registry = CalibrationRegistry([profile("old", age=8 * 24 * 60 * 60)])
    assert registry.select(hardware="rtx-5070-ti", models=["old"], now=NOW) is None
    assert registry.candidates(
        hardware="rtx-5070-ti", models=["old"], now=NOW,
        max_age_seconds=9 * 24 * 60 * 60,
    )[0].model == "old"
