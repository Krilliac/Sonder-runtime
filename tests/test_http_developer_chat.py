"""App chat spellings of ``/test``, ``/digest``, ``/build``, ``/fix-build``,
``/crash`` and ``/profile`` (``serve._handle_slash``).

The app sends its slash line to the HTTP chat dispatcher. These commands used
to fall through it as prose to the model; now each line makes exactly the call
its HTTP route makes -- a typed gateway call as the authenticated principal
(``/v1/tools/test-run``, ``/v1/tools/output-digest``, ``/v1/build/*``) or an
admin debug facade request (``/v1/tools/crash-*``) -- behind the chain's own
permission gate and the route's authority check.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import command_catalog
import permission_modes as pm
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.application.tools.gateway_contract import ToolReceipt
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry
from sonder_runtime.domain.common.errors import Forbidden, SonderError
from sonder_runtime.interfaces.http.facades import developer_chat as chat
from sonder_runtime.interfaces.http.facades.debug_tools import DebugToolsHttpFacade
from tests.test_debug_http_facade import PORTS, PRESENTERS, Service

pytestmark = pytest.mark.unit

JOB = "test-run-" + "0123456789abcdef" * 2
BUILD_JOB = "build-job-" + "0123456789abcdef" * 2
COMMANDS = ("/test", "/digest", "/build", "/fix-build", "/crash", "/profile")

ADMIN = {"authorized": True, "mode": "account", "api_key": False,
         "account": {"role": "admin", "username": "root"}}
DEVELOPER = {"authorized": True, "mode": "account", "api_key": False,
             "account": {"role": "developer", "username": "dev"}}
USER = {"authorized": True, "mode": "account", "api_key": False,
        "account": {"role": "user", "username": "alice"}}


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.AUTO, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


class SpyGateway:
    def __init__(self, outputs=None, *, raises=None):
        self.graph = SimpleNamespace(registry=typed_tool_registry())
        self.requests = []
        self._outputs = list(outputs or [])
        self._raises = raises

    def execute(self, request):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        output = self._outputs.pop(0) if self._outputs else {
            "object": "test_run_status", "job_id": JOB, "status": "running",
            "runner": "pytest", "elapsed_seconds": 0.2, "display_command": ["python", "-m", "pytest"]}
        success = output.get("ok", True) is not False
        return ToolReceipt(request_id=request.request_id, tool_name=request.tool_name,
                           success=success, output=json.dumps(output),
                           error_code="" if success else output.get("error_code", ""),
                           policy_match="permission:allow")


def _serve_gateway(monkeypatch, gateway):
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: SimpleNamespace(tools=gateway, config=None))


def _serve_debug(monkeypatch, service):
    graded = []

    def authorizer(bound):
        def authorize(tool, arguments, context):
            graded.append((tool, dict(arguments), context.source, context.principal_id))
            return "permission:test"
        return authorize

    monkeypatch.setattr(DebugToolsHttpFacade, "_modules", lambda self: (PRESENTERS, PORTS))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: SimpleNamespace(debug_tools=service, config=None))
    monkeypatch.setattr("sonder_runtime.bootstrap.debug_tools.debug_http_authorizer", authorizer)
    return graded


# --- the catalog advertises what the dispatcher serves -----------------------------------------


def test_the_http_index_now_lists_the_developer_commands():
    command_catalog.reset_cache()
    names = {command.name for command in command_catalog.http_catalog()}
    assert set(COMMANDS) <= names
    assert set(COMMANDS) <= command_catalog.http_native_names()
    # Console-only controls are still not advertised to the app.
    assert not {"/model", "/project", "/exit"} & names


def test_the_read_forms_are_graded_by_the_safe_tool_they_reach():
    tools = command_catalog.http_slash_tools()
    narrow = command_catalog.narrow_branch_tools
    assert narrow("/test", "status " + JOB, tools["/test"]) == ("test_run_result",)
    assert narrow("/test", "result " + JOB, tools["/test"]) == ("test_run_result",)
    assert narrow("/test", "k:fast", tools["/test"]) == ("test_run",)
    assert narrow("/test", "cancel " + JOB, tools["/test"]) == ("test_run",)
    assert narrow("/crash", "triage core.1", tools["/crash"]) == ("crash_triage",)
    assert narrow("/crash", "status debug-run-1", tools["/crash"]) == ("debug_run_result",)
    assert narrow("/crash", "cancel debug-run-1", tools["/crash"]) == ("crash_digest",)
    assert narrow("/crash", "core.1 --exe game", tools["/crash"]) == ("crash_digest",)
    assert narrow("/profile", "result debug-run-1", tools["/profile"]) == ("debug_run_result",)
    assert narrow("/profile", "trace.json", tools["/profile"]) == ("profile_capture_digest",)
    # A separator the facade's splitter keeps inside a word is not a read form.
    assert narrow("/crash", "triage core.1", tools["/crash"]) == ("crash_digest",)
    for name in ("test_run_result", "crash_triage", "debug_run_result"):
        assert pm.risk_of(name) == "safe"


# --- parsing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd, arg, tool, arguments", [
    ("/test", "", "test_run", {"runner": "auto", "wait_seconds": 20}),
    ("/test", "pytest k:fast or slow", "test_run",
     {"runner": "pytest", "selector": "k:fast or slow", "wait_seconds": 20}),
    ("/test", "tests/test_a.py::test_b", "test_run",
     {"runner": "auto", "selector": "tests/test_a.py::test_b", "wait_seconds": 20}),
    ("/test", "status " + JOB, "test_run_result", {"job_id": JOB, "wait_seconds": 0}),
    ("/test", "result " + JOB, "test_run_result", {"job_id": JOB, "wait_seconds": 30}),
    ("/digest", "logs/build.log", "output_digest", {"path": "logs/build.log"}),
    ("/digest", JOB, "output_digest", {"job_id": JOB}),
    ("/build", "", "build_model", {}),
    ("/build", "status " + BUILD_JOB, "build_job_result", {"job_id": BUILD_JOB}),
    ("/build", "run game --config Debug", "build_job",
     {"action": "build", "target": "game", "config": "Debug"}),
    ("/fix-build", "game", "build_fix", {"target": "game"}),
])
def test_typed_lines_map_to_one_typed_call(cmd, arg, tool, arguments):
    call = chat.parse_chat_command(cmd, arg)
    assert isinstance(call, chat.TypedCall)
    assert (call.tool, dict(call.arguments)) == (tool, arguments)


def test_a_job_shaped_digest_falls_back_to_a_file_of_that_name():
    call = chat.parse_chat_command("/digest", "build-output")
    assert dict(call.arguments) == {"job_id": "build-output"}
    assert dict(call.fallback.arguments) == {"path": "build-output"}
    assert chat.parse_chat_command("/digest", "a b.log").fallback is None


@pytest.mark.parametrize("cmd, arg, route, payload", [
    ("/crash", "triage core.1", "/v1/tools/crash-triage", {"path": "core.1"}),
    ("/crash", "core.1 --exe game --sym syms --engine gdb", "/v1/tools/crash-digest",
     {"path": "core.1", "executable": "game", "symbol_dirs": ["syms"], "engine": "gdb",
      "symbol_server": False}),
    ("/crash", "core.1 --symbols-online", "/v1/tools/crash-digest",
     {"path": "core.1", "engine": "auto", "symbol_server": True}),
    ("/crash", "status debug-run-1", "/v1/tools/debug-runs/debug-run-1", None),
    ("/crash", "cancel debug-run-1", "/v1/tools/debug-runs/debug-run-1/cancel", None),
    ("/profile", "trace.json --top 10 --budget 16", "/v1/tools/profile-digest",
     {"path": "trace.json", "top_n": 10, "frame_budget_ms": 16}),
])
def test_debug_lines_map_to_one_admin_route(cmd, arg, route, payload):
    call = chat.parse_chat_command(cmd, arg)
    assert isinstance(call, chat.DebugCall)
    assert call.route == route and (dict(call.payload) if call.payload else None) == payload


@pytest.mark.parametrize("cmd, arg, needle", [
    ("/test", "status nope", "usage: /test"),
    ("/test", "cancel " + JOB, "POST /v1/jobs/%s/cancel" % JOB),
    ("/digest", "", "usage: /digest"),
    ("/build", "explode", "usage: /build"),
    ("/fix-build", "", "usage: /fix-build"),
    ("/crash", "", "usage: /crash"),
    ("/crash", "symbols on", "operator console"),
    ("/crash", "fix last", "operator console"),
    ("/crash", "core.1 --repro k:x", "operator console"),
    ("/crash", "core.1 --argv sh", "usage: /crash"),
    ("/profile", "", "usage: /profile"),
])
def test_unserved_forms_answer_with_usage_or_where_to_run_them(cmd, arg, needle):
    with pytest.raises(chat.ChatUsage) as caught:
        chat.parse_chat_command(cmd, arg)
    assert needle in str(caught.value)


# --- the served dispatcher -----------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "/test", "/test pytest k:fast", "/test status " + JOB, "/test result nope",
    "/test cancel " + JOB, "/digest build.log", "/digest", "/build", "/build explode",
    "/fix-build", "/crash", "/crash triage core.1", "/crash symbols on", "/profile",
    "/profile trace.json",
])
@pytest.mark.parametrize("context", [USER, DEVELOPER, ADMIN])
def test_nothing_falls_through_to_the_model_as_prose(monkeypatch, line, context):
    _serve_gateway(monkeypatch, SpyGateway())
    reply = ts._handle_slash(line, context=context)
    assert isinstance(reply, str) and reply


def test_a_caller_without_developer_authority_is_refused_before_the_app(monkeypatch):
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: pytest.fail("a refused chat command built the app"))
    for line in ("/test", "/digest a.log", "/build model", "/fix-build game"):
        assert ts._handle_slash(line, context=USER) == (
            "refused %s: developer or admin authority is required" % line.split()[0])
    for line in ("/crash triage core.1", "/profile trace.json"):
        assert ts._handle_slash(line, context=DEVELOPER) == (
            "refused %s: admin authority is required" % line.split()[0])


def test_test_runs_as_the_account_principal_through_the_gateway(monkeypatch):
    gateway = SpyGateway()
    _serve_gateway(monkeypatch, gateway)
    reply = ts._handle_slash("/test pytest k:fast", context=DEVELOPER)
    assert reply.startswith("test run %s: running (pytest)" % JOB)
    assert "next: /test result " + JOB in reply
    request = gateway.requests[-1]
    assert request.tool_name == "test_run"
    assert dict(request.arguments) == {"runner": "pytest", "selector": "k:fast", "wait_seconds": 20}
    assert request.scope.source == "http" and request.scope.auth_level == "developer"
    assert request.scope.principal_id == "account:" + hashlib.sha256(b"dev").hexdigest()
    assert request.scope.workspace_roots == ()


def test_a_finished_report_is_rendered_with_its_failures(monkeypatch):
    report = {"object": "test_report", "job_id": JOB, "status": "failed", "runner": "pytest",
              "exit_code": 1, "duration_seconds": 1.5,
              "totals": {"passed": 1, "failed": 1, "skipped": 0, "errors": 0, "total": 2},
              "summary_line": "1 failed, 1 passed in 0.1s",
              "failures": [{"id": "t.py::test_bad", "file": "t.py", "line": 4,
                            "message_excerpt": "assert 1 == 2"}]}
    _serve_gateway(monkeypatch, SpyGateway([report]))
    reply = ts._handle_slash("/test result " + JOB, context=DEVELOPER)
    assert "test run %s: failed (pytest) exit=1" % JOB in reply
    assert "passed=1 failed=1" in reply and "t.py::test_bad (t.py:4) assert 1 == 2" in reply


def test_manual_mode_refuses_a_chat_run_at_the_chain_gate_but_not_its_reads(monkeypatch):
    pm.set_mode(pm.MANUAL)
    gateway = SpyGateway([{"object": "test_run_status", "job_id": JOB, "status": "running",
                           "runner": "pytest", "elapsed_seconds": 1}])
    _serve_gateway(monkeypatch, gateway)
    reply = ts._handle_slash("/test pytest", context=DEVELOPER)
    assert reply.startswith("refused /test:")
    assert gateway.requests == []
    reply = ts._handle_slash("/test status " + JOB, context=DEVELOPER)
    assert reply.startswith("test run %s: running" % JOB)
    assert [request.tool_name for request in gateway.requests] == ["test_run_result"]


def test_a_gateway_refusal_names_its_call_id_and_remedies(monkeypatch):
    denied = Forbidden("permission gate refused build_job: unattended")
    denied.decision = {"tool": "build_job", "call_id": "c0ffee12", "source": "unattended"}
    _serve_gateway(monkeypatch, SpyGateway(raises=denied))
    reply = ts._handle_slash("/build run game", context=ADMIN)
    assert reply.startswith("build_job refused: PERMISSION_DENIED")
    assert "call_id: c0ffee12" in reply and "permission_approve" in reply


def test_another_principals_job_digest_falls_back_to_a_file(monkeypatch):
    gateway = SpyGateway([
        {"ok": False, "error_code": "JOB_NOT_FOUND", "message": "job not found"},
        {"object": "output_digest", "source_kind": "file", "final_line": "done"},
    ])
    _serve_gateway(monkeypatch, gateway)
    reply = ts._handle_slash("/digest build-output", context=DEVELOPER)
    assert reply.startswith("output digest:") and '"source_kind": "file"' in reply
    assert [dict(r.arguments) for r in gateway.requests] == [
        {"job_id": "build-output"}, {"path": "build-output"}]


def test_an_unknown_test_run_is_not_found(monkeypatch):
    _serve_gateway(monkeypatch, SpyGateway([
        {"ok": False, "error_code": "JOB_NOT_FOUND", "message": "test run not found"}]))
    reply = ts._handle_slash("/test result " + JOB, context=DEVELOPER)
    assert reply.startswith("test_run_result refused: JOB_NOT_FOUND")


def test_crash_triage_and_digest_go_through_the_admin_facade(monkeypatch):
    service = Service()
    graded = _serve_debug(monkeypatch, service)
    reply = ts._handle_slash("/crash triage core.1", context=ADMIN)
    assert reply.startswith("crash triage:")
    assert service.calls[-1][0] == "triage" and graded == []
    reply = ts._handle_slash("/crash core.1 --exe game", context=ADMIN)
    assert reply.startswith("crash digest:")
    assert service.calls[-1][0] == "crash" and service.calls[-1][-1] == "http"
    # The host launch was graded first, as the admin route grades it.
    assert [(tool, source) for tool, _, source, _ in graded] == [("crash_digest", "http")]
    assert graded[0][3] == "account:" + hashlib.sha256(b"root").hexdigest()


def test_a_chat_crash_digest_keeps_the_payload_cap(monkeypatch):
    service = Service()
    _serve_debug(monkeypatch, service)
    reply = ts._handle_slash("/crash core.1", context=ADMIN)
    assert len(reply) <= 12_000 + 100


def test_symbol_server_stays_console_only_in_chat(monkeypatch):
    service = Service()
    graded = _serve_debug(monkeypatch, service)
    reply = ts._handle_slash("/crash core.1 --symbols-online", context=ADMIN)
    assert reply.startswith("crash digest refused: SYMBOL_SERVER_NEEDS_CONSOLE")
    assert graded == [] and service.calls == []


def test_another_admins_debug_run_is_not_found(monkeypatch):
    service = Service()
    _serve_debug(monkeypatch, service)
    reply = ts._handle_slash("/crash result debug-run-owner", context=ADMIN)
    assert reply.startswith("result refused: JOB_NOT_FOUND")


def test_a_binary_profile_capture_falls_back_to_the_graded_capture_route(monkeypatch):
    class CaptureService(Service):
        def profile_pure(self, request, context):
            self.calls.append(("profile_pure", request))
            if request.path.endswith(".data"):
                error = SonderError("needs a host tool")
                error.code = "CAPTURE_NEEDS_HOST_TOOL"
                raise error
            return SimpleNamespace(label=request.path)

    service = CaptureService()
    graded = _serve_debug(monkeypatch, service)
    reply = ts._handle_slash("/profile trace.json", context=ADMIN)
    assert reply.startswith("profile digest:") and graded == []
    reply = ts._handle_slash("/profile perf.data --exe game", context=ADMIN)
    assert reply.startswith("profile capture:")
    assert [call[0] for call in service.calls] == ["profile_pure", "profile_pure", "profile"]
    assert [tool for tool, *_ in graded] == ["profile_capture_digest"]


def test_the_branch_is_behind_the_chain_gate():
    """The developer branch sits after the one choke point, like every other."""
    import inspect

    source = inspect.getsource(ts._handle_slash)
    gate = source.index("_http_slash_refusal(cmd, arg, context=context)")
    branch = source.index('"/test", "/digest", "/build", "/fix-build", "/crash", "/profile"')
    assert gate < branch
