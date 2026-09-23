from __future__ import annotations

import pytest

from sonder_runtime.application.routing.backend_conformance import (
    DeterministicFakeProvider,
    RecentCapabilityEvidence,
    run_smoke_probes,
)
from sonder_runtime.application.routing.capability_router import (
    CapabilityRoutingError,
    CapabilityRouter,
    RoutingRequest,
)
from sonder_runtime.domain.agents.roles import AgentRole
from sonder_runtime.domain.routing.capability_profiles import Capability, CapabilityProfile


def test_fake_provider_records_plain_structured_and_cancellation(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=60)
    record = run_smoke_probes(DeterministicFakeProvider(), backend="local", model="fixture", now=100)
    evidence.save(record)
    restored = evidence.load("local", "fixture")
    assert restored is not None
    assert {item.capability.value for item in restored.results if item.passed} == {
        "chat", "structured", "cancellation"
    }
    assert restored.synthetic is True


def test_store_preserves_two_models_across_reopen(tmp_path):
    path = tmp_path / "capabilities.json"
    evidence = RecentCapabilityEvidence(path, max_records=2)
    for model in ("fixture-a", "fixture-b"):
        evidence.save(run_smoke_probes(DeterministicFakeProvider(), backend="local", model=model))
    reopened = RecentCapabilityEvidence(path, max_records=2)
    assert reopened.load("local", "fixture-a") is not None
    assert reopened.load("local", "fixture-b") is not None


def test_router_requires_recent_backend_evidence_and_reports_reason(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=60)
    router = CapabilityRouter(
        (CapabilityProfile("fixture", frozenset({Capability.EDIT, Capability.STRUCTURED})),),
        recent_evidence=evidence,
        evidence_clock=lambda: 20,
    )
    try:
        router.route(RoutingRequest(AgentRole.INTEGRATOR))
    except CapabilityRoutingError as exc:
        assert exc.reason_code == "recent_capability_evidence_missing"
    else:
        raise AssertionError("expected missing recent capability evidence")


def test_router_refuses_stale_evidence_then_accepts_recent_record(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=10)
    evidence.save(run_smoke_probes(DeterministicFakeProvider(), backend="local", model="fixture", now=1, synthetic=False))
    allowed, reason = evidence.check("fixture", frozenset({Capability.STRUCTURED}), now=20)
    assert not allowed and reason == "recent_capability_evidence_stale"
    stale_router = CapabilityRouter(
        (CapabilityProfile("fixture", frozenset({Capability.EDIT, Capability.STRUCTURED})),),
        recent_evidence=evidence,
        evidence_clock=lambda: 20,
    )
    with pytest.raises(CapabilityRoutingError) as failure:
        stale_router.route(RoutingRequest(AgentRole.INTEGRATOR))
    assert failure.value.reason_code == "recent_capability_evidence_stale"
    evidence.save(run_smoke_probes(DeterministicFakeProvider(), backend="local", model="fixture", now=20, synthetic=False))
    router = CapabilityRouter(
        (CapabilityProfile("fixture", frozenset({Capability.EDIT, Capability.STRUCTURED})),),
        recent_evidence=evidence,
        evidence_clock=lambda: 20,
    )
    decision = router.route(RoutingRequest(AgentRole.INTEGRATOR))
    assert decision.model == "fixture"


def test_router_rejects_synthetic_and_future_evidence(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=60)
    evidence.save(run_smoke_probes(DeterministicFakeProvider(), backend="local", model="fixture"))
    router = CapabilityRouter(
        (CapabilityProfile("fixture", frozenset({Capability.EDIT, Capability.STRUCTURED})),),
        recent_evidence=evidence,
    )
    with pytest.raises(CapabilityRoutingError) as synthetic:
        router.route(RoutingRequest(AgentRole.INTEGRATOR))
    assert synthetic.value.reason_code == "synthetic_capability_evidence"
    evidence.save(run_smoke_probes(DeterministicFakeProvider(), backend="local", model="fixture", now=10**12, synthetic=False))
    with pytest.raises(CapabilityRoutingError) as future:
        router.route(RoutingRequest(AgentRole.INTEGRATOR))
    assert future.value.reason_code == "future_capability_evidence"


def test_router_uses_profile_backend_identity(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    evidence.save(run_smoke_probes(
        DeterministicFakeProvider(), backend="private-local", model="fixture", synthetic=False
    ))
    router = CapabilityRouter(
        (CapabilityProfile(
            "fixture", frozenset({Capability.EDIT, Capability.STRUCTURED}),
            backend="private-local",
        ),),
        recent_evidence=evidence,
    )
    assert router.route(RoutingRequest(AgentRole.INTEGRATOR)).model == "fixture"
