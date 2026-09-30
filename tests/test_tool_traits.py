"""Tri-state knowledge, legacy compatibility and actual gateway admission."""
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.domain.tools.builtin_traits import builtin_traits
from sonder_runtime.domain.tools.descriptors import ToolEffect
from sonder_runtime.domain.tools.traits import ToolTraits, TriState, traits_from_effects
from sonder_runtime.application.ports.tool_registry import (
    ExecutableToolInventory, InMemoryToolRegistry, ToolDescriptor,
)
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.tools.facade import ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway, ToolGatewayRequest, ToolPermission, ToolScope,
)
from sonder_runtime.application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy
from sonder_runtime.application.tools.scheduling import ToolConcurrencyGate


def test_unknown_is_conservative_in_every_consumer_direction():
    traits = ToolTraits()
    assert traits.read_only is TriState.UNKNOWN
    assert traits.destructive is TriState.UNKNOWN
    assert traits.idempotent is TriState.UNKNOWN
    assert traits.concurrency_safe is TriState.UNKNOWN
    assert traits.open_world is TriState.UNKNOWN
    assert not traits.is_read_only
    assert traits.may_be_destructive
    assert not traits.replay_safe
    assert not traits.can_parallelize
    assert traits.is_open_world


def test_tristate_cannot_silently_coerce_to_boolean():
    for state in TriState:
        with pytest.raises(TypeError):
            bool(state)
    with pytest.raises(TypeError):
        ToolTraits(read_only=True)


@pytest.mark.parametrize("limit", [False, 0, -1, 1.5])
def test_result_advisory_is_positive_integer(limit):
    with pytest.raises(ValueError):
        ToolTraits(max_result_bytes=limit)


def test_advisory_hints_cannot_enable_any_relaxation():
    hints = ToolTraits(read_only=TriState.TRUE, destructive=TriState.FALSE,
                       idempotent=TriState.TRUE, concurrency_safe=TriState.TRUE,
                       open_world=TriState.FALSE, host_declared=False)
    assert not hints.is_read_only
    assert hints.may_be_destructive
    assert not hints.replay_safe
    assert not hints.can_parallelize
    assert hints.is_open_world


def test_read_only_ignores_destructive_hint_but_does_not_infer_replay_or_concurrency():
    traits = ToolTraits(read_only=TriState.TRUE, destructive=TriState.TRUE)
    assert not traits.may_be_destructive
    assert not traits.replay_safe
    assert not traits.can_parallelize


def test_legacy_empty_effects_are_unknown_and_explicit_read_effects_survive():
    assert ToolDescriptor("unknown").traits == ToolTraits()
    assert traits_from_effects([]) == ToolTraits()
    read = traits_from_effects({ToolEffect.READ_FILES})
    assert read.is_read_only and read.replay_safe
    assert not read.is_open_world
    assert not traits_from_effects({ToolEffect.NETWORK}).is_read_only
    assert traits_from_effects({ToolEffect.DELETE_FILES}).may_be_destructive


def test_existing_binary_read_capabilities_keep_host_authority():
    from tool_capabilities import CAPABILITIES, Effect
    for capability in CAPABILITIES.values():
        if capability.effect is Effect.READ_ONLY:
            assert capability.traits.is_read_only, capability.name
            assert not capability.traits.may_be_destructive
            assert builtin_traits(capability.name).is_read_only


def test_native_and_typed_catalogs_keep_traits_and_wire_schemas():
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry
    from sonder_runtime.bootstrap.typed_tools import READ_ONLY_TOOLS, typed_tool_registry
    from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
    native = native_tool_registry()
    typed = typed_tool_registry()
    for descriptor in native.list_all():
        assert isinstance(descriptor.traits, ToolTraits)
    for name in READ_ONLY_TOOLS:
        assert native.get(name).traits.is_read_only
        assert typed.get(name).traits == native.get(name).traits
    assert native.get("write_file").traits.destructive is TriState.UNKNOWN
    assert native.get("file_delete").traits.destructive is TriState.TRUE
    assert not native.get("test_run").traits.replay_safe
    bundle = GeneratedCatalogs.generate(typed)
    for tool in bundle.mcp["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}
        assert tool["inputSchema"] == typed.get(tool["name"]).input_schema
    for tool in bundle.client["tools"]:
        assert set(tool) == {"name", "description", "input_schema", "effects", "execution_class"}
    for tool in bundle.permissions["tools"]:
        assert set(tool) == {"name", "effects", "execution_class"}


def test_inventory_snapshot_preserves_advisory_provenance():
    descriptor = ToolDescriptor("foreign", traits=ToolTraits(read_only=TriState.TRUE, host_declared=False))
    snapshot = ExecutableToolInventory((descriptor,))
    assert snapshot.get("foreign").traits == descriptor.traits
    assert not snapshot.get("foreign").traits.is_read_only


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, descriptor, call, context, execution_class):
        self.calls.append(call)
        return ToolExecutionResult(tool_name=call.tool_name, output="done", success=True)


def _facade(traits, *, rule_effect=""):
    descriptor = ToolDescriptor("operation", traits=traits)
    executor = _Executor()
    policy = ResourcePolicy([PolicyRule("allow-operation", Decision.ALLOW,
                                        tool="operation", side_effect_class=rule_effect)])
    facade = ToolApplicationFacade.compose(InMemoryToolRegistry([descriptor]), executor, policy=policy)
    return facade, executor


def _request(traits=None, reconciliation="manual"):
    return ToolGatewayRequest("request", "operation", {}, ToolScope("principal"),
                              ToolPermission(traits=traits, reconciliation=reconciliation))


def test_gateway_uses_registered_traits_instead_of_caller_claims():
    facade, executor = _facade(ToolTraits())
    optimistic = ToolTraits(read_only=TriState.TRUE, idempotent=TriState.TRUE)
    with pytest.raises(Forbidden, match="idempotent"):
        facade.execute(_request(optimistic, "idempotent"))
    assert not executor.calls
    assert facade.receipts[-1].terminal == "policy_denied"


def test_gateway_allows_authoritative_idempotent_contract():
    facade, executor = _facade(ToolTraits(idempotent=TriState.TRUE))
    assert facade.execute(_request(reconciliation="idempotent")).success
    assert len(executor.calls) == 1


def test_raw_gateway_does_not_accept_a_request_claim_of_host_authority():
    receipts = []
    calls = []
    gateway = ToolGateway(
        SimpleNamespace(validate=lambda *args: None),
        SimpleNamespace(authorize=lambda *args: None),
        SimpleNamespace(approve=lambda request: True),
        SimpleNamespace(invoke=calls.append),
        SimpleNamespace(redact=lambda name, value: value),
        SimpleNamespace(record=receipts.append),
    )
    with pytest.raises(Forbidden, match="idempotent"):
        gateway.execute(_request(ToolTraits(idempotent=TriState.TRUE), "idempotent"))
    assert not calls
    assert receipts[-1].terminal == "policy_denied"


@pytest.mark.parametrize("open_world,allowed", [(TriState.UNKNOWN, False),
                                               (TriState.TRUE, False),
                                               (TriState.FALSE, True)])
def test_resource_rules_treat_unknown_external_reach_as_network(open_world, allowed):
    descriptor = ToolDescriptor("read", effects=frozenset({ToolEffect.READ_FILES}),
                                traits=ToolTraits(read_only=TriState.TRUE, open_world=open_world))
    executor = _Executor()
    policy = ResourcePolicy([PolicyRule("local-reads", Decision.ALLOW,
                                        tool="read", side_effect_class="read_files")])
    facade = ToolApplicationFacade.compose(InMemoryToolRegistry([descriptor]), executor, policy=policy)
    request = ToolGatewayRequest("read", "read", {},
                                 ToolScope("principal", allowed_effects=frozenset({"read_files"})),
                                 ToolPermission(frozenset({"read_files"})))
    if allowed:
        assert facade.execute(request).success
    else:
        with pytest.raises(Forbidden):
            facade.execute(request)
        assert not executor.calls


@pytest.mark.parametrize("first_parallel,second_parallel,overlap", [
    (False, True, False), (True, False, False), (False, False, False), (True, True, True),
])
def test_concurrent_admission_requires_both_contracts(first_parallel, second_parallel, overlap):
    gate = ToolConcurrencyGate()
    entered = Event()
    attempting = Event()
    release = Event()
    errors = []

    def second():
        try:
            attempting.set()
            with gate.admit(second_parallel):
                entered.set()
                assert release.wait(3)
        except BaseException as exc:
            errors.append(exc)

    with gate.admit(first_parallel):
        thread = Thread(target=second)
        thread.start()
        assert attempting.wait(3)
        if overlap:
            assert entered.wait(3)
        else:
            assert not entered.wait(0.05)
        release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert entered.is_set()
    assert not errors
