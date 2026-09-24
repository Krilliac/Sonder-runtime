"""Measured backend evidence must bind actual routes, never model names alone."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.model_gateway.facade import ModelGatewayFacade
from sonder_runtime.application.model_gateway.health_and_roles import (
    LogicalRole,
    ProviderHealth,
    ProviderState,
    RoleBinding,
)
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
    run_protocol_probes,
)
from sonder_runtime.application.routing.capability_router import (
    CapabilityRouter,
    CapabilityRoutingError,
    RoutingRequest,
)
from sonder_runtime.domain.agents.roles import AgentRole
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    EvidenceState,
    ProbeResult,
)
from sonder_runtime.domain.routing.capability_profiles import (
    Capability,
    CapabilityProfile,
)


def identity(**changes):
    return replace(BackendIdentity(
        backend="local", model="model:latest", model_digest="a" * 64,
        quantization="Q4_K_M", backend_version="0.13.2",
        tokenizer_digest="b" * 64, template_digest="c" * 64,
        context_tokens=32768, hardware="cpu:x86_64/16GiB",
    ), **changes)


def record(*results, at=100, instance=None):
    return BackendConformanceRecord(
        "local", "model:latest", at, tuple(results), identity=instance or identity()
    )


def passed(capability):
    return ProbeResult(capability, True, "observed")


def test_identity_bound_evidence_rotates_when_any_execution_input_changes(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=60)
    observed = identity()
    evidence.save(record(passed(BackendCapability.CHAT)))
    assert evidence.load("local", "model:latest").identity == observed
    for rotated in (
        identity(model_digest="d" * 64), identity(quantization="Q8_0"),
        identity(backend_version="0.13.3"), identity(tokenizer_digest="e" * 64),
        identity(template_digest="f" * 64), identity(context_tokens=8192),
        identity(hardware="gpu:RTX-4060"),
    ):
        verdict = evidence.assess(
            "model:latest", frozenset({BackendCapability.CHAT}),
            backend="local", identity=rotated, now=110,
        )
        assert verdict.state is EvidenceState.UNKNOWN
        assert verdict.reason_code == "backend_identity_changed"
    assert evidence.assess(
        "model:latest", frozenset({BackendCapability.CHAT}),
        backend="local", identity=observed, now=110,
    ).state is EvidenceState.PASSED


def test_oversized_or_invalid_evidence_store_refuses_without_reading_unbounded_data(tmp_path):
    path = tmp_path / "capabilities.json"
    evidence = RecentCapabilityEvidence(path)
    path.write_bytes(b" " * (RecentCapabilityEvidence.MAX_STORE_BYTES + 1))
    assert evidence.assess(
        "model:latest", frozenset(), backend="local", identity=identity(), now=100,
    ).state is EvidenceState.UNKNOWN
    with pytest.raises(ValueError, match="bounded"):
        RecentCapabilityEvidence(path, max_records=65)


def test_record_unknown_failed_stale_and_synthetic_never_certify(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=10)
    args = {"backend": "local", "identity": identity(), "now": 100}
    required = frozenset({BackendCapability.CHAT, BackendCapability.TOOL_CONTINUATION})
    evidence.save(record(passed(BackendCapability.CHAT)))
    verdict = evidence.assess("model:latest", required, **args)
    assert verdict.state is EvidenceState.UNKNOWN
    assert verdict.capabilities[BackendCapability.TOOL_CONTINUATION] is EvidenceState.UNKNOWN

    evidence.save(record(passed(BackendCapability.CHAT), ProbeResult(
        BackendCapability.TOOL_CONTINUATION, False, "tool_result_missing",
    )))
    verdict = evidence.assess("model:latest", required, **args)
    assert verdict.state is EvidenceState.FAILED
    assert verdict.reason_code == "recent_capability_probe_failed"

    evidence.save(record(passed(BackendCapability.CHAT), passed(
        BackendCapability.TOOL_CONTINUATION,
    )))
    assert evidence.assess("model:latest", required, **args).state is EvidenceState.PASSED
    assert evidence.assess("model:latest", required, backend="local", identity=identity(),
                           now=111).state is EvidenceState.STALE
    evidence.save(replace(record(*map(passed, required)), synthetic=True))
    assert evidence.assess("model:latest", required, **args).state is EvidenceState.UNKNOWN


def test_tool_route_requires_measured_invocation_and_sequence_not_only_a_label(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    router = CapabilityRouter(
        (CapabilityProfile("model:latest", frozenset({Capability.TOOLS, Capability.EDIT})),),
        recent_evidence=evidence, identity_for=lambda profile: identity(),
        evidence_clock=lambda: 100,
    )
    needed = (BackendCapability.CHAT, BackendCapability.CODING,
              BackendCapability.TOOL_CONTINUATION, BackendCapability.TOOL_SEQUENTIAL)
    evidence.save(record(*map(passed, needed)))
    with pytest.raises(CapabilityRoutingError):
        router.route(RoutingRequest(AgentRole.EDITOR, required=frozenset({Capability.TOOLS})))
    evidence.save(record(*map(passed, needed), passed(BackendCapability.TOOL_FALLBACK)))
    assert router.route(RoutingRequest(
        AgentRole.EDITOR, required=frozenset({Capability.TOOLS}),
    )).model == "model:latest"


def test_live_protocol_cases_validate_transcript_shape_and_leave_unsupported_unknown():
    observed = {
        BackendCapability.TOOL_SEQUENTIAL: {
            "model": "model:latest", "events": ["call:a", "result:a", "call:b", "result:b"],
        },
        BackendCapability.TOOL_PARALLEL: {
            "model": "model:latest", "parallel_max": 2,
            "events": ["call:a", "call:b", "result:a", "result:b"],
        },
        BackendCapability.TOOL_CONTINUATION: {
            "model": "model:latest", "events": ["call:echo", "result:echo", "assistant:done"],
        },
        BackendCapability.LONG_CONTEXT: {
            "model": "model:latest", "input_tokens": 8192,
            "start_marker": True, "end_marker": True,
        },
    }

    class LiveProtocol:
        def run_case(self, capability, *, model, timeout_seconds):
            assert model == "model:latest" and timeout_seconds <= 30
            return observed.get(capability)

    result = run_protocol_probes(LiveProtocol(), identity=identity(), now=100)
    assert result.passed == frozenset(observed)
    assert result.synthetic is True
    assert BackendCapability.RESUME not in result.passed
    assert BackendCapability.RESUME not in result.failed
    assert result.identity == identity()

    observed[BackendCapability.TOOL_CONTINUATION] = {
        "model": "other-model", "events": ["call:echo", "result:echo", "assistant:done"],
    }
    different = run_protocol_probes(LiveProtocol(), identity=identity(), now=100)
    assert BackendCapability.TOOL_CONTINUATION in different.failed
    with pytest.raises(ValueError, match="cannot certify live inference"):
        run_protocol_probes(LiveProtocol(), identity=identity(), synthetic=False)


def test_router_rejects_name_only_record_and_allows_identity_bound_role(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=30)
    model = CapabilityProfile("model:latest", frozenset({Capability.EDIT}), backend="local")
    router = CapabilityRouter((model,), recent_evidence=evidence,
                              identity_for=lambda profile: identity(),
                              evidence_clock=lambda: 100)
    evidence.save(BackendConformanceRecord(
        "local", "model:latest", 100,
        (passed(BackendCapability.CHAT), passed(BackendCapability.CODING)),
    ))
    with pytest.raises(CapabilityRoutingError, match="identity") as missing:
        router.route(RoutingRequest(AgentRole.EDITOR))
    assert missing.value.reason_code == "backend_identity_missing"
    evidence.save(record(passed(BackendCapability.CHAT), passed(BackendCapability.CODING)))
    assert router.route(RoutingRequest(AgentRole.EDITOR)).model == "model:latest"


def test_verified_escalation_preserves_role_budget_and_attributes_actual_lift(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    models = ("model:latest", "model:strong")
    identities = {name: identity(model=name, model_digest=hex(index + 10)[2:] * 64)
                  for index, name in enumerate(models)}
    for model in models:
        evidence.save(BackendConformanceRecord(
            "local", model, 100,
            (passed(BackendCapability.CHAT), passed(BackendCapability.CODING)),
            identity=identities[model],
        ))
    router = CapabilityRouter(
        (CapabilityProfile(models[0], frozenset({Capability.EDIT}), escalation_rank=0),
         CapabilityProfile(models[1], frozenset({Capability.EDIT}), escalation_rank=1)),
        recent_evidence=evidence,
        identity_for=lambda profile: identities[profile.model],
        evidence_clock=lambda: 100,
    )
    initial = router.route(RoutingRequest(AgentRole.EDITOR, uncertainty=1.0))
    assert initial.model == models[0] and initial.escalated is False
    assert initial.evidence_reason == "recent_capability_evidence_passed"
    stuck = router.route(RoutingRequest(
        AgentRole.EDITOR, escalation_count=1, previous_model=models[0],
        verifier_passed=True,
    ))
    assert stuck.model == models[0] and stuck.escalated is False
    escalated = router.route(RoutingRequest(
        AgentRole.EDITOR, previous_model=models[0], verifier_passed=False,
        spent_tokens=1000, spent_wall_seconds=5,
    ))
    assert escalated.model == models[1] and escalated.reason == "verifier_failure"
    assert escalated.budget.output_tokens == 5000
    assert escalated.budget.wall_seconds == 595
    outcome = escalated.attribute_outcome(verifier_passed=True, tokens_used=60, wall_seconds=10)
    assert outcome.helped is True and outcome.total_tokens == 1060
    assert outcome.total_wall_seconds == 15 and outcome.within_budget is True
    with pytest.raises(CapabilityRoutingError) as exhausted:
        router.route(RoutingRequest(AgentRole.EDITOR, spent_tokens=6000))
    assert exhausted.value.reason_code == "route_budget_exhausted"
    with pytest.raises(CapabilityRoutingError) as unobserved:
        router.route(RoutingRequest(AgentRole.EDITOR, verifier_passed=False))
    assert unobserved.value.reason_code == "prior_route_missing"


def test_opt_in_gateway_refuses_role_without_recent_evidence_before_provider_call(tmp_path):
    class Gateway:
        def __init__(self):
            self.calls = 0

        def generate(self, request, context):
            self.calls += 1
            return ModelResponse("ok", "model:latest", request.tier)

        def embed(self, texts, context):
            raise AssertionError("embedding without measured evidence must be refused")

    gateway = Gateway()
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json", max_age_seconds=60)
    health = {"local": ProviderHealth(
        "local", ProviderState.READY, datetime.now(timezone.utc),
    )}
    facade = ModelGatewayFacade(
        gateway, health=health,
        bindings={
            LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "local", "model:latest"),
            LogicalRole.VERIFIER: RoleBinding(LogicalRole.VERIFIER, "local", "model:latest"),
        },
        recent_evidence=evidence, identity_for=lambda route: identity(),
        evidence_clock=lambda: 100,
    )
    context = local_owner_context(correlation_id="conformance-route")
    request = ModelRequest("hello", "model:latest")
    with pytest.raises(DependencyUnavailable, match="evidence_missing"):
        facade.generate_for_role(request, context)
    with pytest.raises(DependencyUnavailable, match="evidence_missing"):
        facade.generate(request, context)
    with pytest.raises(DependencyUnavailable, match="evidence_missing"):
        facade.embed(["hello"], context)
    assert gateway.calls == 0

    evidence.save(record(passed(BackendCapability.CHAT)))
    assert facade.generate_for_role(request, context).text == "ok"
    assert facade.generate(request, context).text == "ok"
    with pytest.raises(DependencyUnavailable, match="evidence_missing"):
        facade.embed(["hello"], context)
    with pytest.raises(DependencyUnavailable, match="evidence_missing"):
        facade.generate_for_role(request, context, role=LogicalRole.VERIFIER)
    assert gateway.calls == 2

    evidence.save(record(passed(BackendCapability.CHAT), passed(BackendCapability.STRUCTURED)))
    assert facade.generate_for_role(request, context, role=LogicalRole.VERIFIER).text == "ok"
    with pytest.raises(DependencyUnavailable, match="requested model"):
        facade.generate_for_role(ModelRequest("hello", "other-model"), context)
    assert gateway.calls == 3

    observed = iter((identity(), identity(template_digest="d" * 64)))
    changing = ModelGatewayFacade(
        gateway, health=health,
        bindings={LogicalRole.DEFAULT: RoleBinding(
            LogicalRole.DEFAULT, "local", "model:latest",
        )},
        recent_evidence=evidence, identity_for=lambda route: next(observed),
        evidence_clock=lambda: 100,
    )
    with pytest.raises(DependencyUnavailable, match="backend_identity_changed"):
        changing.generate_for_role(request, context)
    assert gateway.calls == 3


@pytest.mark.parametrize("operation", ("generate", "generate_for_role", "embed"))
def test_opt_in_gateway_rejects_inflight_identity_rotation_even_when_new_probe_passes(
    tmp_path, operation,
):
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    current = {"identity": identity()}
    second = identity(model_digest="d" * 64)
    capabilities = (passed(BackendCapability.CHAT), passed(BackendCapability.EMBEDDING))
    evidence.save(record(*capabilities))

    class RotatingGateway:
        def rotate(self):
            evidence.save(record(*capabilities, instance=second))
            current["identity"] = second

        def generate(self, request, context):
            self.rotate()
            return ModelResponse("from first deployment", "model:latest", request.tier)

        def embed(self, texts, context):
            from sonder_runtime.application.ports.model_gateway import Embedding
            self.rotate()
            return [Embedding((1.0,), "model:latest")]

    provider = RotatingGateway()
    facade = ModelGatewayFacade(
        provider,
        health={"local": ProviderHealth(
            "local", ProviderState.READY, datetime.now(timezone.utc),
        )},
        bindings={LogicalRole.DEFAULT: RoleBinding(
            LogicalRole.DEFAULT, "local", "model:latest",
        )},
        recent_evidence=evidence, identity_for=lambda route: current["identity"],
        evidence_clock=lambda: 100,
    )
    assert facade.gateway is facade  # public compatibility getter must retain opt-in gate
    context = local_owner_context(correlation_id="inflight-identity")
    with pytest.raises(DependencyUnavailable, match="identity changed"):
        if operation == "embed":
            facade.embed(["hello"], context)
        elif operation == "generate_for_role":
            facade.generate_for_role(ModelRequest("hello", "model:latest"), context)
        else:
            facade.generate(ModelRequest("hello", "model:latest"), context)


def test_opt_in_gateway_rejects_a_different_bound_provider(tmp_path):
    class Gateway:
        def generate(self, request, context):
            raise AssertionError("route used a gateway other than the evidenced provider")

    with pytest.raises(ValueError, match="same concrete provider"):
        ModelGatewayFacade(
            Gateway(), providers={"local": Gateway()},
            recent_evidence=RecentCapabilityEvidence(tmp_path / "evidence.json"),
            identity_for=lambda route: identity(),
        )
