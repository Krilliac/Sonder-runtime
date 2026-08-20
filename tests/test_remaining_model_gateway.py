from datetime import datetime, timezone

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.model_gateway.health_and_roles import (
    GatewayBudget,
    LogicalRole,
    ModelGatewayContract,
    ModelParameters,
    NpuBoundary,
    ProviderHealth,
    ProviderState,
    RoleBinding,
    RoleBudgetBook,
)
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import DependencyUnavailable


NOW = datetime.now(timezone.utc)


def health(provider="local", state=ProviderState.READY, *, npu=None):
    return ProviderHealth(provider, state, NOW, npu=npu or NpuBoundary())


class FakeGateway:
    def __init__(self):
        self.calls = []

    def generate(self, request, context):
        self.calls.append((request, context))
        return ModelResponse("ok", "qwen", request.tier)


def test_provider_state_requires_explicit_routable_health():
    fake = FakeGateway()
    contract = ModelGatewayContract(
        {"local": fake},
        {LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "local", "qwen3:30b")},
        health={"local": health(state=ProviderState.UNKNOWN)},
    )
    with pytest.raises(DependencyUnavailable):
        contract.route()
    contract.publish_health(health())
    assert contract.route().health.state is ProviderState.READY


def test_roles_have_separate_budgets_and_updates_are_snapshot_based():
    book = RoleBudgetBook()
    editor = GatewayBudget(max_output_tokens=9000, timeout_seconds=600)
    updated = book.with_budget(LogicalRole.EDITOR, editor)
    assert book.for_role(LogicalRole.EDITOR) != updated.for_role(LogicalRole.EDITOR)
    assert updated.for_role(LogicalRole.EDITOR) is editor
    assert updated.for_role(LogicalRole.REVIEWER) != editor


def test_moe_keeps_total_residency_and_active_compute_counts_distinct():
    params = ModelParameters.from_tag("qwen3:30b-a3b-q4_K_M")
    assert params.total_b == 30
    assert params.active_b == 3
    assert params.is_moe
    with pytest.raises(ValueError):
        ModelParameters(3, 30)


def test_npu_detection_does_not_claim_runtime_readiness():
    detected = NpuBoundary(detected=True, runtime_available=False)
    assert detected.status == "detected-unavailable"
    assert not detected.ready
    with pytest.raises(ValueError):
        NpuBoundary(detected=False, runtime_available=True)


def test_npu_required_role_fails_until_provider_binding_is_evidenced():
    fake = FakeGateway()
    contract = ModelGatewayContract(
        {"npu": fake},
        {LogicalRole.VERIFIER: RoleBinding(LogicalRole.VERIFIER, "npu", "model:7b", GatewayBudget(), True)},
        health={"npu": health(npu=NpuBoundary(detected=True, runtime_available=True))},
    )
    with pytest.raises(DependencyUnavailable, match="detected-unbound"):
        contract.route(LogicalRole.VERIFIER)
    contract.publish_health(health("npu", npu=NpuBoundary(True, True, True, "broker")))
    assert contract.route(LogicalRole.VERIFIER).health.npu.ready


def test_one_gateway_contract_routes_and_delegates_without_transport_knowledge():
    fake = FakeGateway()
    contract = ModelGatewayContract(
        {"local": fake},
        {LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "local", "qwen3:30b")},
        health={"local": health()},
    )
    request = ModelRequest("hello", "sonder")
    response = contract.generate(
        request,
        local_owner_context(correlation_id="model-gateway-test"),
        role=LogicalRole.DEFAULT,
    )
    assert response.text == "ok"
    assert len(fake.calls) == 1
