"""Permission grades for the debug tools, the console-only network rule and
approvals bound to the host-resolved debug plan."""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.adapters.debug_tools_executor import DebugToolExecutor
from sonder_runtime.adapters.developer_tools_executor import DeveloperToolExecutor
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.bootstrap.debug_tools import DebugToolPermissionEvaluator
from sonder_runtime.bootstrap.typed_tools import POLICY_NAMES, typed_tool_policy, typed_tool_registry
from sonder_runtime.domain.common.errors import Forbidden
from tests.test_debug_service import FakePlanner, Stack, plan_for, step
from tests.test_tools_test_runs_fakes import services as developer_services

pytestmark = pytest.mark.unit

DEBUG_SAFE = ("crash_triage", "profile_digest", "debug_run_result")
DEBUG_EXECUTION = ("crash_digest", "profile_capture_digest")


class DigestPlanner(FakePlanner):
    """Plans whose digest depends on the request and the file, like the real planner's."""

    def plan_crash(self, request, context, *, network_allowed, identity, tier0):
        base = super().plan_crash(request, context, network_allowed=network_allowed,
                                  identity=identity, tier0=tier0)
        material = json.dumps([request.path, request.engine, request.executable,
                               list(request.symbol_dirs), identity.sha256, network_allowed])
        return replace(base, command_digest=hashlib.sha256(material.encode()).hexdigest())


class AllowPolicy:
    """Records what the permission modes would be asked, and allows."""

    def __init__(self) -> None:
        self.asked = []

    def decide_for_caller(self, name, **kwargs):
        self.asked.append((name, kwargs.get("arguments")))
        return SimpleNamespace(action="allow", mode="auto", risk="execution", source="mode",
                               reason="", call_id="")

    def allow_action(self):
        return "allow"


def debug_stack():
    stack = Stack()
    stack.planner = DigestPlanner()
    return stack


def evaluator(stack=None):
    stack = stack or debug_stack()
    judge = DebugToolPermissionEvaluator(developer_services(), stack.service(),
                                         policy_names=POLICY_NAMES)
    policy = AllowPolicy()
    judge._policy = policy
    return judge, policy, stack


def request(tool, arguments, *, source="mcp", gate="gateway"):
    return ToolGatewayRequest(
        request_id="req-1", tool_name=tool, arguments=arguments,
        scope=ToolScope(principal_id="owner", source=source, gate=gate,
                        allowed_effects=frozenset({"read_files", "write_files", "execute", "network"})),
        permission=ToolPermission(frozenset()),
    )


# -- grades ---------------------------------------------------------------------


def test_risk_grades():
    for name in DEBUG_EXECUTION:
        assert pm.risk_of(name) == "execution", name
    for name in DEBUG_SAFE:
        assert pm.risk_of(name) == "safe", name


def test_the_execution_grade_is_what_the_gate_reads(monkeypatch):
    import command_catalog

    monkeypatch.setattr(pm, "EXECUTION_TOOLS", pm.EXECUTION_TOOLS - {"crash_digest"})
    command_catalog.reset_cache()
    try:
        assert pm.risk_of("crash_digest") != "execution"
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()
    assert pm.risk_of("crash_digest") == "execution"


# -- the console-only network rule ------------------------------------------------


@pytest.mark.parametrize("source", ["mcp", "http", "worker", "system", "repl"])
@pytest.mark.parametrize("mode", [pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO, "bypass"])
def test_symbol_server_through_the_gateway_is_refused_in_every_mode(monkeypatch, source, mode):
    """Regression for the unattended ASK->ALLOW degrade: the rule is not a risk grade."""
    monkeypatch.setattr(pm, "current_mode", lambda: mode)
    judge, policy, stack = evaluator()
    stack.consent.value = True
    with pytest.raises(Forbidden) as caught:
        judge.authorize_request(request("crash_digest", {"path": "/w/core.1", "symbol_server": True},
                                        source=source))
    assert caught.value.policy_match == "debug:network-console-only"
    assert caught.value.decision["error_code"] == "SYMBOL_SERVER_NEEDS_CONSOLE"
    assert policy.asked == [] and stack.planner.calls == []


def test_symbol_server_is_refused_even_on_a_surface_decided_request():
    judge, policy, _ = evaluator()
    with pytest.raises(Forbidden) as caught:
        judge.authorize_request(request("crash_digest", {"path": "/w/c", "symbol_server": True},
                                        gate="surface"))
    assert caught.value.policy_match == "debug:network-console-only"


def test_without_the_rule_auto_mode_would_allow_the_same_call(monkeypatch):
    """Mutation proof: the modes alone grade crash_digest execution, which auto allows."""
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    decision = pm.decide_for_caller("crash_digest", interactive=False, gate_control_exempt=False,
                                    surface="native-mcp", mode=pm.AUTO, record=False,
                                    arguments={"path": "/w/c", "symbol_server": True})
    assert decision.action == pm.ALLOW


# -- plan binding -------------------------------------------------------------------


def test_the_resolved_command_is_the_hosts_and_a_callers_is_discarded():
    judge, policy, stack = evaluator()
    forged = {"command_digest": "0" * 64, "display_argvs": [["/bin/true"]]}
    judge.authorize_request(request("crash_digest", {"path": "/w/core.1",
                                                     "resolved_command": forged}))
    name, arguments = policy.asked[-1]
    assert name == "crash_digest"
    resolved = arguments["resolved_command"]
    assert resolved != forged and resolved["command_digest"] != "0" * 64
    assert resolved["display_argvs"] == [["/usr/bin/gdb", "{input}"]]
    assert resolved["input_sha256"] == "ab" * 32 and resolved["network"] is False


def test_two_plans_of_the_same_request_bind_the_same_digest_and_a_change_rebinds():
    judge, policy, stack = evaluator()
    arguments = {"path": "/w/core.1", "engine": "gdb", "executable": "/w/app"}
    judge.authorize_request(request("crash_digest", dict(arguments)))
    judge.authorize_request(request("crash_digest", dict(arguments)))
    first, second = (asked[1]["resolved_command"]["command_digest"] for asked in policy.asked[-2:])
    assert first == second
    judge.authorize_request(request("crash_digest", {**arguments, "engine": "lldb"}))
    assert policy.asked[-1][1]["resolved_command"]["command_digest"] != first
    assert pm.call_digest("crash_digest", policy.asked[-3][1]) == pm.call_digest(
        "crash_digest", policy.asked[-2][1])


def test_profile_capture_digest_is_plan_bound_too():
    judge, policy, stack = evaluator()
    stack.source.kind = "perf_data"
    judge.authorize_request(request("profile_capture_digest", {"path": "/w/perf.data"}))
    assert "resolved_command" in policy.asked[-1][1]


@pytest.mark.parametrize("tool", DEBUG_SAFE)
def test_the_pure_tools_are_not_planned(tool):
    judge, policy, stack = evaluator()
    judge.authorize_request(request(tool, {"path": "/w/core.1", "run_id": "debug-run-" + "a" * 32}))
    assert stack.planner.calls == []
    assert "resolved_command" not in (policy.asked[-1][1] or {})


def test_a_refused_plan_is_refused_before_anyone_is_asked():
    from sonder_runtime.application.debugging.ports import debug_error

    judge, policy, stack = evaluator()
    stack.planner.refusal = debug_error("ENGINE_REFUSED_MANAGED_DUMP", "clr.dll in the dump")
    with pytest.raises(Forbidden) as caught:
        judge.authorize_request(request("crash_digest", {"path": "/w/core.1", "engine": "cdb"}))
    assert caught.value.policy_match == "debug:plan-refused"
    assert caught.value.decision == {"tool": "crash_digest",
                                     "error_code": "ENGINE_REFUSED_MANAGED_DUMP", "stage": "plan"}
    assert policy.asked == []


def test_bad_arguments_are_refused_at_planning():
    judge, policy, _ = evaluator()
    with pytest.raises(Forbidden) as caught:
        judge.authorize_request(request("crash_digest", {"path": "/w/c", "engine": "windbg"}))
    assert caught.value.decision["error_code"] == "INVALID_INPUT"


def test_surface_decided_requests_are_not_replanned():
    judge, policy, stack = evaluator()
    assert judge.authorize_request(request("crash_digest", {"path": "/w/c"}, gate="surface")) \
        == "permission:surface"
    assert stack.planner.calls == []


def test_test_run_keeps_the_developer_binding():
    judge, policy, _ = evaluator()
    judge.authorize_request(request("test_run", {"runner": "pytest"}))
    assert policy.asked[-1][1]["resolved_command"]["runner"] == "pytest"


def test_an_uncomposed_debug_service_still_strips_a_forged_command():
    judge = DebugToolPermissionEvaluator(developer_services(), None, policy_names=POLICY_NAMES)
    policy = AllowPolicy()
    judge._policy = policy
    judge.authorize_request(request("crash_digest", {"path": "/w/c", "resolved_command": {"x": 1}}))
    assert "resolved_command" not in policy.asked[-1][1]


# -- through the native MCP surface ------------------------------------------------------


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    pm.reset_unattended_for_tests()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


def native(tmp_path, calls, stack):
    from sonder_runtime.bootstrap.native_mcp import run_native_mcp

    developer = developer_services()
    service = stack.service()
    audit = DurableToolAuditRepository(tmp_path / "audit.jsonl")
    tools = ToolApplicationFacade.compose(
        typed_tool_registry(),
        DebugToolExecutor(service, DeveloperToolExecutor(developer, PackagedToolExecutor(),
                                                         inventory_wire=lambda view: view)),
        policy=typed_tool_policy(), receipts=ReceiptStore(), audit=audit,
        permissions=(DebugToolPermissionEvaluator(developer, service, policy_names=POLICY_NAMES),),
    )
    app = SimpleNamespace(config=None, tools=tools, tool_audit=audit, developer_tools=developer,
                          debug_tools=service)
    messages = [("initialize", {"protocolVersion": "2.0", "capabilities": {}})] + list(calls)
    stream = io.StringIO("".join(
        json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params}) + "\n"
        for index, (method, params) in enumerate(messages)))
    output = io.StringIO()
    run_native_mcp(app, input_stream=stream, output_stream=output)
    return [json.loads(row) for row in output.getvalue().splitlines()][1:]


def _call(name, arguments):
    return ("tools/call", {"name": name, "arguments": arguments})


@pytest.mark.parametrize("mode", [pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS])
def test_an_unattended_crash_digest_is_refused_outside_auto(tmp_path, monkeypatch, ledger, mode):
    monkeypatch.setattr(pm, "current_mode", lambda: mode)
    stack = debug_stack()
    replies = native(tmp_path, [_call("crash_digest", {"path": "/w/core.1"})], stack)
    assert replies[0]["result"]["isError"] and replies[0]["result"]["error"] == "permission_denied"
    assert stack.launcher.started == []


def test_auto_runs_it_with_the_host_plan(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    stack = debug_stack()
    stack.launcher.auto_finish = None
    replies = native(tmp_path, [_call("crash_digest", {"path": "/w/core.1", "wait_seconds": 0})], stack)
    result = replies[0]["result"]
    assert result["isError"] is False, result
    assert json.loads(result["output"])["status"] == "running"
    assert len(stack.launcher.started) == 1


def test_auto_refuses_symbol_server_from_mcp(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    stack = debug_stack()
    stack.consent.value = True
    replies = native(tmp_path, [_call("crash_digest", {"path": "/w/core.1", "symbol_server": True})],
                     stack)
    result = replies[0]["result"]
    assert result["isError"] and result["error"] == "permission_denied"
    assert stack.launcher.started == [] and stack.planner.calls == []


def test_the_result_poll_is_safe_in_plan_mode(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    replies = native(tmp_path, [_call("debug_run_result", {"run_id": "debug-run-" + "c" * 32})],
                     debug_stack())
    result = replies[0]["result"]
    assert result.get("error") != "permission_denied"
    assert json.loads(result["output"])["error_code"] == "JOB_NOT_FOUND"


def test_the_digest_plan_double_is_deterministic():
    stack = debug_stack()
    ident = __import__("tests.test_debug_service", fromlist=["identity"]).identity()
    one = plan_for(ident, (step(),))
    assert one.resolved_command()["display_argvs"] == [["/usr/bin/gdb", "{input}"]]


def test_both_typed_facades_carry_the_debug_resolvers_next_to_the_build_ones():
    """The main facade and the lane-tests facade grade crash_digest and
    profile_capture_digest on the planned command, with the build tools'
    resolvers and grant authority in the same evaluator."""
    import inspect

    from sonder_runtime.bootstrap import app as bootstrap_app

    source = inspect.getsource(bootstrap_app.build_application)
    assert source.count("**debug_permission_resolvers(debug_tools)") == 2
    assert source.count("_debug_executor_chain(debug_tools, developer_tools)") == 2
    assert source.count("grant_authorities=(build_grants,)") == 2
