from __future__ import annotations

import pytest

from sonder_runtime.domain.routing.capability_profiles import Capability, CapabilityProfile


def test_capability_profile_round_trips_as_provider_profile_record():
    profile = CapabilityProfile(
        "qwen3.5-30b-q4",
        frozenset({Capability.EDIT, Capability.VERIFY}),
        quality=0.91,
        latency_ms=240,
        context_tokens=32768,
        escalation_rank=1,
    )
    restored = CapabilityProfile.from_dict(profile.to_dict())

    assert restored == profile
    assert restored.digest() == profile.digest()
    assert profile.to_dict()["capabilities"] == ["edit", "verify"]


def test_unknown_capability_is_rejected_before_routing():
    with pytest.raises(ValueError, match="unknown"):
        CapabilityProfile.from_dict({"model": "local", "capabilities": ["telepathy"]})
