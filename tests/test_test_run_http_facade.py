"""``/v1/tools/test-run`` and ``/v1/tools/output-digest`` over the typed gateway.

Routing and refusals are proved against a spy gateway (exactly what reaches
it); the permission grading and ownership against the real typed gateway over
the real structured-test-run stack (a real pytest process) and the real
output digest service; and the served handler's developer-authority gate over
a live HTTP server.
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.application.developer_tools import DeveloperToolServices
from sonder_runtime.application.tools.gateway_contract import ToolReceipt
from sonder_runtime.bootstrap.diagnostics import compose_output_digest_service
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.interfaces.http.facades.testing_tools import (
    PERMISSION_REMEDIES,
    TestRunHttpRoutes,
    owns,
    route_call,
)
from sonder_runtime.platform.logging import Redactor
from tests.test_compute_job_http import _post
from tests.test_compute_snapshot_http import _get, http_server  # noqa: F401 - fixture
from tests.test_tools_test_runs_fakes import FakeDigest, FakeInventory, FakeTestRuns, facade
from tests.test_tools_test_runs_harness import stack  # noqa: F401 - fixture

pytestmark = pytest.mark.unit

JOB = "test-run-" + "0123456789abcdef" * 2


class SpyGateway:
    def __init__(self, output=None, *, success=True, error_code="", raises=None):
        self.graph = SimpleNamespace(registry=typed_tool_registry())
        self.requests = []
        self._output = output if output is not None else {
            "object": "test_run_status", "job_id": JOB, "status": "running"}
        self._success = success
        self._code = error_code
        self._raises = raises

    def execute(self, request):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return ToolReceipt(request_id=request.request_id, tool_name=request.tool_name,
                           success=self._success, output=json.dumps(self._output),
                           error_code=self._code, policy_match="permission:allow")


def dispatch(gateway, method, path, query=None, payload=None, principal="account:abc"):
    return TestRunHttpRoutes(lambda: gateway).dispatch(
        method, path, query or {}, payload, principal_id=principal, workspace_roots=("/w",))


# --- routing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("method, path, query, payload, tool, arguments", [
    ("POST", "/v1/tools/test-run", {}, {"runner": "pytest", "selector": "k:fast", "wait_seconds": 0},
     "test_run", {"runner": "pytest", "selector": "k:fast", "wait_seconds": 0}),
    ("POST", "/v1/tools/test-run", {}, None, "test_run", {}),
    ("GET", "/v1/tools/test-run/" + JOB, {"wait_seconds": ["10"]}, None, "test_run_result",
     {"job_id": JOB, "wait_seconds": 10}),
    ("POST", "/v1/tools/output-digest", {}, {"job_id": JOB, "tail_lines": 5}, "output_digest",
     {"job_id": JOB, "tail_lines": 5}),
    ("POST", "/v1/tools/output-digest", {}, {"path": "build.log"}, "output_digest",
     {"path": "build.log"}),
])
def test_routes_map_to_one_typed_call(method, path, query, payload, tool, arguments):
    assert route_call(method, path, query, payload) == (tool, arguments)


def test_the_facade_owns_only_its_routes():
    for path in ("/v1/tools/test-run", "/v1/tools/test-run/" + JOB, "/v1/tools/output-digest"):
        assert owns(path)
    for path in ("/v1/tools/inventory", "/v1/tools/crash-digest", "/v1/tools/test-runs",
                 "/v1/tools/debug-runs/x", "/v1/build/jobs", "/v1/jobs/" + JOB + "/cancel"):
        assert not owns(path)


@pytest.mark.parametrize("method, path, query, payload, status", [
    ("GET", "/v1/tools/test-run", {}, None, 405),
    ("GET", "/v1/tools/output-digest", {}, None, 405),
    ("POST", "/v1/tools/test-run/" + JOB, {}, {}, 405),
    ("GET", "/v1/tools/test-run/build-job-0123", {}, None, 404),
    ("GET", "/v1/tools/test-run/%s/cancel" % JOB, {}, None, 404),
    ("POST", "/v1/tools/test-run", {}, {"argv": ["-p", "evil"]}, 400),
    ("POST", "/v1/tools/test-run", {}, {"extra_args_json": "[\"-c\", \"x\"]"}, 400),
    ("POST", "/v1/tools/test-run", {}, ["pytest"], 400),
    ("POST", "/v1/tools/test-run", {"runner": ["pytest"]}, {}, 400),
    ("GET", "/v1/tools/test-run/" + JOB, {"wait_seconds": ["-1"]}, None, 400),
    ("GET", "/v1/tools/test-run/" + JOB, {"wait_seconds": ["1", "2"]}, None, 400),
    ("POST", "/v1/tools/output-digest", {}, {"job_id": JOB, "path": "build.log"}, 400),
    ("POST", "/v1/tools/output-digest", {}, {"tail_lines": 5}, 400),
    ("POST", "/v1/tools/output-digest", {}, {"path": "a.log", "operator": True}, 400),
])
def test_malformed_requests_never_reach_the_gateway(method, path, query, payload, status):
    gateway = SpyGateway()
    code, body = dispatch(gateway, method, path, query, payload)
    assert code == status and "error" in body
    assert gateway.requests == []


def test_principal_source_and_202_semantics():
    gateway = SpyGateway()
    status, body = dispatch(gateway, "POST", "/v1/tools/test-run", payload={"runner": "pytest"})
    assert status == 202 and body["job_id"] == JOB
    request = gateway.requests[0]
    assert request.tool_name == "test_run"
    assert request.scope.principal_id == "account:abc"
    assert request.scope.source == "http" and request.scope.gate == "gateway"
    assert request.scope.workspace_roots == ("/w",)
    assert request.scope.allowed_effects == frozenset({"read_files", "write_files", "execute"})
    assert body["receipt"]["request_id"] == request.request_id
    report = SpyGateway({"object": "test_report", "job_id": JOB, "status": "failed"})
    assert dispatch(report, "POST", "/v1/tools/test-run", payload={})[0] == 200
    assert dispatch(report, "GET", "/v1/tools/test-run/" + JOB)[0] == 200
    running = SpyGateway()
    assert dispatch(running, "GET", "/v1/tools/test-run/" + JOB)[0] == 202


@pytest.mark.parametrize("code, status", [
    ("JOB_NOT_FOUND", 404), ("DEVELOPER_TOOLS_UNAVAILABLE", 503), ("TEST_RUN_BUSY", 429),
    ("PROJECT_OUTSIDE_ROOTS", 403), ("DIGEST_SOURCE_REJECTED", 403), ("INVALID_SELECTOR", 400),
    ("RUNNER_UNAVAILABLE", 503),
])
def test_typed_failures_map_to_statuses(code, status):
    gateway = SpyGateway({"ok": False, "error_code": code, "message": "m"}, success=False,
                         error_code=code)
    got, body = dispatch(gateway, "GET", "/v1/tools/test-run/" + JOB)
    assert got == status and body["error"]["code"] == code


def test_permission_refusals_carry_the_remedies_and_plan_refusals_their_code():
    denied = Forbidden("permission gate refused test_run: unattended")
    denied.decision = {"tool": "test_run", "call_id": "abc", "source": "unattended"}
    status, body = dispatch(SpyGateway(raises=denied), "POST", "/v1/tools/test-run", payload={})
    assert status == 403 and body["error"]["code"] == "PERMISSION_DENIED"
    assert body["error"]["remedies"] == list(PERMISSION_REMEDIES)
    assert body["error"]["decision"]["call_id"] == "abc"
    refused = Forbidden("test_run refused before execution")
    refused.decision = {"tool": "test_run", "error_code": "INVALID_SELECTOR", "stage": "plan"}
    status, body = dispatch(SpyGateway(raises=refused), "POST", "/v1/tools/test-run", payload={})
    assert status == 400 and body["error"]["code"] == "INVALID_SELECTOR"


def test_no_gateway_is_developer_tools_unavailable():
    status, body = TestRunHttpRoutes(lambda: None).dispatch(
        "POST", "/v1/tools/test-run", {}, {}, principal_id="owner")
    assert status == 503 and body["error"]["code"] == "DEVELOPER_TOOLS_UNAVAILABLE"


def test_uncomposed_developer_services_are_503(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    from sonder_runtime.adapters.developer_tools_executor import DeveloperToolExecutor
    from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
    from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
    from sonder_runtime.bootstrap.typed_tools import typed_tool_policy

    tools = ToolApplicationFacade.compose(
        typed_tool_registry(), DeveloperToolExecutor(None, PackagedToolExecutor()),
        policy=typed_tool_policy(), receipts=ReceiptStore())
    status, body = TestRunHttpRoutes(lambda: tools).dispatch(
        "GET", "/v1/tools/test-run/" + JOB, {}, None, principal_id="owner")
    assert status == 503 and body["error"]["code"] == "DEVELOPER_TOOLS_UNAVAILABLE"


# --- the real gateway: grading --------------------------------------------------------------------


@pytest.fixture
def graded(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    pm.reset_unattended_for_tests()
    yield
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


@pytest.mark.parametrize("mode", [pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS])
def test_an_unattended_http_run_is_refused_with_its_call_id(tmp_path, monkeypatch, graded, mode):
    monkeypatch.setattr(pm, "current_mode", lambda: mode)
    developer = DeveloperToolServices(inventory=FakeInventory(), test_runs=FakeTestRuns(),
                                      digest=FakeDigest())
    tools, _, _ = facade(tmp_path, developer)
    status, body = TestRunHttpRoutes(lambda: tools).dispatch(
        "POST", "/v1/tools/test-run", {}, {"runner": "pytest"}, principal_id="owner")
    assert status == 403 and body["error"]["code"] == "PERMISSION_DENIED"
    assert body["error"]["decision"]["call_id"]
    assert body["error"]["remedies"] == list(PERMISSION_REMEDIES)
    assert developer.test_runs.runs == []
    # The safe reads still answer in the same mode.
    status, body = TestRunHttpRoutes(lambda: tools).dispatch(
        "POST", "/v1/tools/output-digest", {}, {"path": "build.log"}, principal_id="owner")
    assert status == 200 and body["source_kind"] == "file"


def test_a_refused_http_run_can_be_approved_once_by_its_call_id(tmp_path, monkeypatch):
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger

    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
    pm.reset_unattended_for_tests()
    try:
        tools, _, developer = facade(tmp_path)
        routes = TestRunHttpRoutes(lambda: tools)
        body_in = {"runner": "pytest", "selector": "k:fast"}
        status, body = routes.dispatch("POST", "/v1/tools/test-run", {}, body_in,
                                       principal_id="owner")
        assert status == 403
        pending = ledger.resolve_call(body["error"]["decision"]["call_id"])
        assert pending.tool == "test_run"
        ledger.issue(pending.tool, pending.digest, approver="test")
        status, body = routes.dispatch("POST", "/v1/tools/test-run", {}, body_in,
                                       principal_id="owner")
        assert status == 202, body
        assert [request.selector for request, _, _ in developer.test_runs.runs] == ["k:fast"]
        # Spent: the same call is refused again.
        assert routes.dispatch("POST", "/v1/tools/test-run", {}, body_in,
                               principal_id="owner")[0] == 403
    finally:
        pm.forget_spent_approval()
        pm.reset_unattended_for_tests()


# --- the real gateway over a real pytest run ---------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="the real runner stack reads /proc")
def test_a_real_run_202_then_200_owner_scoped_with_digests(tmp_path, monkeypatch, graded, stack):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    project = stack.allowed / "proj"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "test_mod.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert 1 == 2\n")
    (stack.allowed / "build.log").write_text("step 1\nerror: boom\nFAILED test_x\n")
    digest = compose_output_digest_service(lambda: stack.registry, redactor=Redactor(env={}))
    developer = DeveloperToolServices(inventory=FakeInventory(), test_runs=stack.service,
                                      digest=digest)
    tools, _, _ = facade(tmp_path, developer)
    routes = TestRunHttpRoutes(lambda: tools)

    status, body = routes.dispatch("POST", "/v1/tools/test-run", {},
                                   {"project": str(project), "runner": "pytest", "wait_seconds": 0},
                                   principal_id="owner")
    assert status == 202, body
    assert body["object"] == "test_run_status" and body["ok"] is True
    job_id = body["job_id"]

    deadline = time.monotonic() + 90
    while True:
        status, body = routes.dispatch("GET", "/v1/tools/test-run/" + job_id,
                                       {"wait_seconds": ["5"]}, None, principal_id="owner")
        if status != 202 or time.monotonic() > deadline:
            break
    assert status == 200, body
    assert body["object"] == "test_report" and body["status"] == "failed"
    assert body["exit_code"] == 1 and body["totals"]["passed"] == 1

    # Another principal cannot read, or digest, this run.
    status, body = routes.dispatch("GET", "/v1/tools/test-run/" + job_id, {}, None,
                                   principal_id="account:other")
    assert status == 404 and body["error"]["code"] == "JOB_NOT_FOUND"
    status, body = routes.dispatch("POST", "/v1/tools/output-digest", {}, {"job_id": job_id},
                                   principal_id="account:other")
    assert status == 404 and body["error"]["code"] == "JOB_NOT_FOUND"

    status, body = routes.dispatch("POST", "/v1/tools/output-digest", {},
                                   {"job_id": job_id, "tail_lines": 5}, principal_id="owner")
    assert status == 200 and body["source_kind"] == "job"
    status, body = routes.dispatch("POST", "/v1/tools/output-digest", {},
                                   {"path": str(stack.allowed / "build.log")}, principal_id="owner")
    assert status == 200 and body["source_kind"] == "file"
    assert "error: boom" in json.dumps(body)
    outside = tmp_path / "outside.log"
    outside.write_text("secret-ish\n")
    status, body = routes.dispatch("POST", "/v1/tools/output-digest", {},
                                   {"path": str(outside)}, principal_id="owner")
    assert status == 403 and body["error"]["code"] == "DIGEST_SOURCE_REJECTED"


# --- the served handler ---------------------------------------------------------------------------


def _serve_as(monkeypatch, *, authorized, role, username="alice"):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": authorized, "mode": "account",
        "account": {"role": role, "username": username}, "api_key": False})


@pytest.mark.parametrize("role", ["user", "developer"])
def test_serve_refuses_a_caller_without_admin_authority(http_server, monkeypatch, role):
    # A test run executes the project's own code and a digest reads any
    # log-like file under the roots; a non-admin carries no workspace grant
    # to confine either, so the family is admin-only, like /v1/tools/crash-*.
    _serve_as(monkeypatch, authorized=True, role=role)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: pytest.fail("a refused request constructed the app"))
    status, body = _post(http_server, "/v1/tools/test-run", {"runner": "pytest"})
    assert status == 403 and body["error"]["code"] == "FORBIDDEN"
    assert body["error"]["message"] == "admin authority is required"
    assert _get(http_server, "/v1/tools/test-run/" + JOB)[0] == 403
    assert _post(http_server, "/v1/tools/output-digest", {"path": "a.log"})[0] == 403


def test_serve_dispatches_as_the_account_principal(http_server, monkeypatch, tmp_path):
    import hashlib

    gateway = SpyGateway()
    _serve_as(monkeypatch, authorized=True, role="admin", username="root")
    state = SimpleNamespace(workspace_roots=(str(tmp_path),))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: SimpleNamespace(tools=gateway, config=SimpleNamespace(state=state)))
    status, body = _post(http_server, "/v1/tools/test-run", {"runner": "pytest"})
    assert status == 202 and body["job_id"] == JOB
    request = gateway.requests[-1]
    assert request.scope.source == "http" and request.scope.auth_level == "admin"
    # The admin caller carries the configured workspace grant to the planner.
    assert tuple(str(root) for root in request.scope.workspace_roots) == (
        str(tmp_path.resolve()),)
    assert request.scope.principal_id == "account:" + hashlib.sha256(b"root").hexdigest()
    status, body = _post(http_server, "/v1/tools/output-digest", {"job_id": JOB, "path": "a.log"})
    assert status == 400 and body["error"]["code"] == "INVALID_TEST_REQUEST"
    assert len(gateway.requests) == 1


def test_serve_routes_the_family_on_get_and_post():
    import inspect

    from sonder_runtime.interfaces.http import serve

    source = inspect.getsource(serve.Handler._handle_test_tools_request)
    assert "TestRunHttpRoutes" in source and "_dispatch_developer_gateway_route(" in source
    assert '_handle_test_tools_request("GET", path)' in inspect.getsource(serve.Handler.do_GET)
    # do_POST opens the per-request turn scope and delegates the routing.
    assert "self._handle_post_request()" in inspect.getsource(serve.Handler.do_POST)
    assert '_handle_test_tools_request("POST", path, req)' in inspect.getsource(
        serve.Handler._handle_post_request
    )
