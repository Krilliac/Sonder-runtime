"""``/v1/build/*``: routing, principal propagation, 202 semantics, refusals.

Every route is one typed gateway call with ``source="http"`` as the
authenticated principal; a spy gateway proves exactly what reaches it, and a
real gateway over the service doubles proves the manual-mode refusal and its
remedies.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.application.tools.gateway_contract import ToolReceipt
from sonder_runtime.bootstrap.build_tools import register_build_http_routes
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.interfaces.http.facades.build_tools import (
    PERMISSION_REMEDIES,
    BuildHttpRoutes,
    route_call,
)
from tests.test_build_executor import compose_facade, fake_services, port_doubles  # noqa: F401

pytestmark = pytest.mark.unit

JOB = "build-job-" + "0123456789abcdef" * 2
FIX = "build-fix-" + "0123456789abcdef" * 2


class SpyGateway:
    def __init__(self, output=None, *, success=True, error_code="", raises=None):
        self.graph = SimpleNamespace(registry=typed_tool_registry())
        self.requests = []
        self._output = output if output is not None else {"object": "build_job_status",
                                                           "job_id": JOB, "status": "running"}
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
    return BuildHttpRoutes(lambda: gateway).dispatch(
        method, path, query or {}, payload, principal_id=principal, workspace_roots=("/w",))


@pytest.mark.parametrize("method, path, query, payload, tool, arguments", [
    ("GET", "/v1/build/model", {"detail": ["targets"], "refresh": ["true"], "max_items": ["5"]}, None,
     "build_model", {"detail": "targets", "refresh": True, "max_items": 5}),
    ("POST", "/v1/build/jobs", {}, {"target": "game", "config": "Debug"}, "build_job",
     {"target": "game", "config": "Debug"}),
    ("GET", "/v1/build/jobs/" + JOB, {"wait_seconds": ["10"]}, None, "build_job_result",
     {"job_id": JOB, "wait_seconds": 10}),
    ("POST", "/v1/build/jobs/%s/cancel" % JOB, {}, {}, "build_job_result", {"job_id": JOB, "cancel": True}),
    ("POST", "/v1/build/fix", {}, {"target": "game", "attempts": 3}, "build_fix",
     {"target": "game", "attempts": 3}),
    ("GET", "/v1/build/fix/" + FIX, {}, None, "build_fix_result", {"job_id": FIX}),
    ("POST", "/v1/build/fix/%s/cancel" % FIX, {}, {}, "build_fix_result", {"job_id": FIX, "cancel": True}),
    ("POST", "/v1/build/fix/%s/restore" % FIX, {}, {"files": ["src/a.cpp"]}, "build_fix_restore",
     {"job_id": FIX, "files": ["src/a.cpp"]}),
])
def test_routes_map_to_one_typed_call(method, path, query, payload, tool, arguments):
    assert route_call(method, path, query, payload) == (tool, arguments)


@pytest.mark.parametrize("method, path, query, payload, status", [
    ("POST", "/v1/build/model", {}, {}, 405),
    ("GET", "/v1/build/jobs", {}, None, 405),
    ("GET", "/v1/build/nope", {}, None, 404),
    ("GET", "/v1/build/jobs/build-job-XYZ", {}, None, 404),
    ("GET", "/v1/build/model", {"bogus": ["1"]}, None, 400),
    ("GET", "/v1/build/model", {"detail": ["a", "b"]}, None, 400),
    ("GET", "/v1/build/model", {"max_items": ["-1"]}, None, 400),
    ("POST", "/v1/build/jobs", {}, {"command": "rm -rf /"}, 400),
    ("POST", "/v1/build/jobs", {}, ["target"], 400),
    ("POST", "/v1/build/fix/%s/restore" % FIX, {}, {"files": [], "extra": 1}, 400),
])
def test_malformed_requests_never_reach_the_gateway(method, path, query, payload, status):
    gateway = SpyGateway()
    code, body = dispatch(gateway, method, path, query, payload)
    assert code == status and "error" in body
    assert gateway.requests == []


def test_principal_source_and_202_semantics():
    gateway = SpyGateway()
    status, body = dispatch(gateway, "POST", "/v1/build/jobs", payload={"target": "game"})
    assert status == 202 and body["job_id"] == JOB
    request = gateway.requests[0]
    assert request.tool_name == "build_job"
    assert request.scope.principal_id == "account:abc"
    assert request.scope.source == "http" and request.scope.gate == "gateway"
    assert request.scope.workspace_roots == ("/w",)
    assert request.scope.allowed_effects == frozenset({"read_files", "write_files", "execute"})
    assert body["receipt"]["request_id"] == request.request_id
    report = SpyGateway({"object": "build_job_report", "job_id": JOB, "status": "failed"})
    assert dispatch(report, "POST", "/v1/build/jobs", payload={"target": "game"})[0] == 200
    fix = SpyGateway({"object": "build_fix_status", "job_id": FIX, "status": "running"})
    assert dispatch(fix, "POST", "/v1/build/fix", payload={"target": "game"})[0] == 202


@pytest.mark.parametrize("code, status", [
    ("JOB_NOT_FOUND", 404), ("BUILD_TOOLS_UNAVAILABLE", 503), ("BUILD_DIR_BUSY", 409),
    ("BUILD_BUSY", 429), ("UNKNOWN_TARGET", 400), ("RESTORE_CONFLICT", 409),
    ("UTILITY_TARGET_REFUSED", 403),
])
def test_typed_failures_map_to_statuses(code, status):
    gateway = SpyGateway({"ok": False, "error_code": code, "message": "m"}, success=False,
                         error_code=code)
    got, body = dispatch(gateway, "GET", "/v1/build/jobs/" + JOB)
    assert got == status and body["error"]["code"] == code


def test_permission_refusals_carry_the_remedies_and_plan_refusals_their_code():
    denied = Forbidden("permission gate refused build_job: unattended")
    denied.decision = {"tool": "build_job", "call_id": "abc", "source": "unattended"}
    status, body = dispatch(SpyGateway(raises=denied), "POST", "/v1/build/jobs", payload={})
    assert status == 403 and body["error"]["code"] == "PERMISSION_DENIED"
    assert body["error"]["remedies"] == list(PERMISSION_REMEDIES)
    assert body["error"]["decision"]["call_id"] == "abc"
    refused = Forbidden("build_job refused before execution")
    refused.decision = {"tool": "build_job", "error_code": "UNKNOWN_TARGET", "stage": "plan"}
    status, body = dispatch(SpyGateway(raises=refused), "POST", "/v1/build/jobs", payload={})
    assert status == 400 and body["error"]["code"] == "UNKNOWN_TARGET"


def test_no_gateway_is_unavailable():
    status, body = BuildHttpRoutes(lambda: None).dispatch(
        "GET", "/v1/build/model", {}, None, principal_id="owner")
    assert status == 503 and body["error"]["code"] == "BUILD_TOOLS_UNAVAILABLE"
    assert isinstance(register_build_http_routes(lambda: None), BuildHttpRoutes)


def test_manual_mode_refuses_http_builds_and_auto_runs_them(tmp_path, port_doubles, monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, _ = compose_facade(tmp_path, services)
    routes = BuildHttpRoutes(lambda: tools)
    monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
    status, body = routes.dispatch("POST", "/v1/build/jobs", {}, {"target": "game"},
                                   principal_id="owner")
    assert status == 403 and body["error"]["code"] == "PERMISSION_DENIED"
    assert body["error"]["remedies"]
    assert services.jobs.runs == []
    status, body = routes.dispatch("GET", "/v1/build/model", {}, None, principal_id="owner")
    assert status == 200 and body["object"] == "build_model"
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    status, body = routes.dispatch("POST", "/v1/build/jobs", {}, {"target": "game"},
                                   principal_id="owner")
    assert status == 202, body
    assert services.jobs.runs[-1][2] == "owner"
    job_id = body["job_id"]
    status, body = routes.dispatch("GET", "/v1/build/jobs/" + job_id, {}, None,
                                   principal_id="account:other")
    assert status == 404 and body["error"]["code"] == "JOB_NOT_FOUND"


def test_serve_registers_the_build_routes_behind_developer_authority():
    import inspect

    from sonder_runtime.interfaces.http import serve

    source = inspect.getsource(serve.Handler._handle_build_request)
    assert "_developer_authorized(auth)" in source
    assert "BuildHttpRoutes" in source
    get_source = inspect.getsource(serve.Handler.do_GET)
    post_source = inspect.getsource(serve.Handler.do_POST)
    assert '_handle_build_request("GET", path)' in get_source
    assert '_handle_build_request("POST", path, req)' in post_source
