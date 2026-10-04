"""The resume gate changes authority for a bound worker, never tool risk grades."""
import pytest

import permission_modes as pm
import server
from sonder_runtime.bootstrap.native_mcp import native_tool_registry
from sonder_runtime.application.execution.resume_reality import ResumeBarrier, bound_resume_barrier


def test_every_builtin_permission_decision_is_unchanged_without_a_barrier(monkeypatch):
    from sonder_runtime.adapters.workspace_reality import GitWorkspaceReality

    def no_probe(*args, **kwargs):
        raise AssertionError("a permission decision must never probe Git")

    monkeypatch.setattr(GitWorkspaceReality, "capture", no_probe)
    monkeypatch.setattr(GitWorkspaceReality, "revalidate", no_probe)
    names = sorted({tool.name for tool in server.mcp._tool_manager.list_tools()}
                   | {tool.name for tool in native_tool_registry().list_all()})
    checked = 0
    for name in names:
        for mode in pm._MATRIX:
            for interactive in (False, True):
                kwargs = dict(mode=mode, interactive=interactive, record=False, rule_lookup=lambda _: None)
                before = pm.decide(name, **kwargs)
                with bound_resume_barrier(ResumeBarrier(None)):
                    after = pm.decide(name, **kwargs)
                assert (before.action, before.risk, before.source) == (after.action, after.risk, after.source)
                checked += 1
    print(f"native_builtins={len(native_tool_registry().list_all())} "
          f"legacy_builtins={len(server.mcp._tool_manager.list_tools())} "
          f"distinct_names={len(names)} unchanged_permission_cases={checked} git_probes=0")


@pytest.mark.parametrize("mode", tuple(pm._MATRIX))
def test_all_effect_classes_remain_blocked_even_with_allow_rules(mode):
    names = sorted({tool.name for tool in server.mcp._tool_manager.list_tools()}
                   | {tool.name for tool in native_tool_registry().list_all()})
    before = {name: pm.risk_of(name) for name in names}
    barrier = ResumeBarrier({"requires_reinspection": True, "requires_replan": True})
    with bound_resume_barrier(barrier):
        for name, risk in before.items():
            if risk in pm.UNATTENDED_REFUSED_RISKS:
                decision = pm.decide(name, mode=mode, interactive=False, record=False,
                                     rule_lookup=lambda _: "allow")
                assert decision.action == pm.DENY
                assert decision.source == "fence"
            assert pm.risk_of(name) == risk
    assert {name: pm.risk_of(name) for name in names} == before


def test_native_descriptor_census_allows_reads_but_blocks_effects_at_executor():
    from types import SimpleNamespace
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
    from sonder_runtime.application.tools.typed_gateway import PortBackedToolInvoker
    from sonder_runtime.application.execution.resume_reality import ResumeMutationBlocked
    from sonder_runtime.domain.tools.descriptors import ToolEffect

    registry = native_tool_registry()
    invoked = []
    class Executor:
        def execute(self, descriptor, *args):
            invoked.append(descriptor.name)
            return ToolExecutionResult(descriptor.name, True, output={})
    policy = SimpleNamespace(authorize=lambda *args: None,
                             select_execution_class=lambda descriptor: descriptor.execution_class)
    invoker = PortBackedToolInvoker(registry, policy, Executor(),
                                   context_factory=lambda _: local_owner_context(correlation_id="census"))
    barrier = ResumeBarrier({"requires_reinspection": True, "requires_replan": True})
    reads = mutations = 0
    with bound_resume_barrier(barrier):
        for descriptor in registry.list_all():
            request = SimpleNamespace(tool_name=descriptor.name, arguments={}, request_id="census")
            if descriptor.effects - {ToolEffect.READ_FILES}:
                with pytest.raises(ResumeMutationBlocked):
                    invoker.invoke(request)
                mutations += 1
            else:
                assert invoker.invoke(request).success
                reads += 1
    assert len(invoked) == reads
    print(f"native_descriptors={reads + mutations} reads_allowed={reads} mutators_blocked={mutations}")
