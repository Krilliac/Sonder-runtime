"""Admin HTTP facade for crash/profile digests, plus the interfaces layering rule."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.errors import CapacityExceeded, NotFound
from sonder_runtime.interfaces.http.facades import debug_tools as http_facade
from sonder_runtime.interfaces.http.facades.debug_tools import DebugToolsHttpFacade
from tests.test_compute_job_http import _post
from tests.test_compute_snapshot_http import _get, http_server  # noqa: F401 - fixture


REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CrashDigestRequest:
    path: str
    executable: str = ""
    symbol_dirs: tuple = ()
    engine: str = "auto"
    symbol_server: bool = False
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class CrashTriageRequest:
    path: str
    max_threads: int = 16
    max_files: int = 64


@dataclass(frozen=True)
class ProfileDigestRequest:
    path: str
    executable: str = ""
    engine: str = "auto"
    symbol_dirs: tuple = ()
    top_n: int = 25
    frame_budget_ms: int | None = None
    thread: str = ""
    frame_zone: str = ""
    timeout_seconds: int | None = None


PORTS = SimpleNamespace(CrashDigestRequest=CrashDigestRequest,
                        CrashTriageRequest=CrashTriageRequest,
                        ProfileDigestRequest=ProfileDigestRequest)


def _report_to_wire(report, max_bytes=48_000):
    # Spec: the presenter halves lists to fit max_bytes (fit_wire style).
    frames = list(report.frames)
    while len(json.dumps(frames)) > max_bytes - 1_000 and frames:
        frames = frames[: len(frames) // 2]
    return {"schema": "sonder.crash_report/1", "signature": report.signature,
            "frames": frames, "truncated": len(frames) != len(report.frames)}


PRESENTERS = SimpleNamespace(
    report_to_wire=_report_to_wire,
    digest_to_wire=lambda digest: {"schema": "sonder.profile_digest/1", "label": digest.label},
)


@dataclass
class Outcome:
    run_id: str
    status: str
    crash: object = None
    profile: object = None
    error_code: str = ""
    notes: tuple = ()


class Service:
    def __init__(self):
        self.calls = []
        self.owner_runs = {"debug-run-owner": "owner"}

    def triage(self, request, context):
        self.calls.append(("triage", request))
        if request.path.endswith("/"):
            return (SimpleNamespace(signature="a" * 16, basis="functions", count=2,
                                    exception_name="SIGSEGV", top_frame="game!f",
                                    sample_labels=("x.core",)),)
        return SimpleNamespace(signature="s1", frames=("f",))

    def crash(self, request, context, *, wait_seconds=60, console_confirmed=False):
        self.calls.append(("crash", request, wait_seconds, console_confirmed, context.source))
        if request.symbol_server:
            return Outcome("", "refused", error_code="SYMBOL_SERVER_NEEDS_CONSOLE")
        big = tuple("frame %05d %s" % (i, "x" * 80) for i in range(5_000))
        return Outcome("debug-run-owner", "complete",
                       crash=SimpleNamespace(signature="s2", frames=big))

    def profile_pure(self, request, context):
        self.calls.append(("profile_pure", request))
        return SimpleNamespace(label=request.path)

    def profile(self, request, context, *, wait_seconds=60):
        self.calls.append(("profile", request, wait_seconds))
        return Outcome("debug-run-p", "running")

    def result(self, run_id, context, *, wait_seconds=0):
        if self.owner_runs.get(run_id) != context.principal_id:
            raise NotFound("not yours")
        return Outcome(run_id, "complete", crash=SimpleNamespace(signature="s3", frames=()))

    def cancel(self, run_id, context):
        if self.owner_runs.get(run_id) != context.principal_id:
            raise NotFound("not yours")
        return Outcome(run_id, "cancelled")


def _facade(service=None):
    service = service or Service()
    return DebugToolsHttpFacade(lambda: service, presenters=PRESENTERS, ports=PORTS), service


def _ctx(principal="owner"):
    from dataclasses import replace

    return replace(local_owner_context(correlation_id="h1", source="http"),
                   principal_id=principal)


# --- admin guard and symbol server ------------------------------------------------------


@pytest.mark.parametrize("method, route, payload", [
    ("POST", "/v1/tools/crash-triage", {"path": "x.dmp"}),
    ("POST", "/v1/tools/crash-digest", {"path": "x.dmp"}),
    ("POST", "/v1/tools/crash-digest", {"path": "x.dmp", "symbol_server": True}),
    ("POST", "/v1/tools/profile-digest", {"path": "t.json"}),
    ("POST", "/v1/tools/profile-capture-digest", {"path": "perf.data"}),
    ("GET", "/v1/tools/debug-runs/debug-run-owner", None),
    ("POST", "/v1/tools/debug-runs/debug-run-owner/cancel", None),
])
def test_non_admin_is_forbidden_before_the_service(method, route, payload):
    facade, service = _facade()
    status, body = facade.dispatch(method, route, payload, _ctx(), admin=False)
    assert (status, body["error_code"]) == (403, "FORBIDDEN")
    assert service.calls == []


def test_symbol_server_over_http_is_refused_with_needs_console():
    facade, service = _facade()
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest",
                                   {"path": "x.dmp", "symbol_server": True}, _ctx(), admin=True)
    assert status == 403 and body == {"ok": False, "error_code": "SYMBOL_SERVER_NEEDS_CONSOLE",
                                      "error": {"code": "SYMBOL_SERVER_NEEDS_CONSOLE"}}
    assert service.calls == []


@pytest.mark.parametrize("value", [True, 1, 0, "false", "", [], {}, 0.0])
def test_anything_but_json_false_for_symbol_server_is_refused(value):
    facade, service = _facade()
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest",
                                   {"path": "x.dmp", "symbol_server": value}, _ctx(), admin=True)
    assert (status, body["error_code"]) == (403, "SYMBOL_SERVER_NEEDS_CONSOLE")
    assert service.calls == []


def test_service_refusal_code_maps_to_403_too():
    facade, _ = _facade()
    status, body = facade._outcome(Outcome("", "refused", error_code="SYMBOL_SERVER_NEEDS_CONSOLE"),
                                   PRESENTERS)
    assert status == 403 and body["error_code"] == "SYMBOL_SERVER_NEEDS_CONSOLE"


# --- runs ---------------------------------------------------------------------------------------


def test_another_principals_run_is_not_found():
    facade, _ = _facade()
    for method, route in (("GET", "/v1/tools/debug-runs/debug-run-owner"),
                          ("POST", "/v1/tools/debug-runs/debug-run-owner/cancel")):
        status, body = facade.dispatch(method, route, None, _ctx("account:someone"), admin=True)
        assert (status, body["error_code"]) == (404, "JOB_NOT_FOUND")
    status, body = facade.dispatch("GET", "/v1/tools/debug-runs/debug-run-owner", None, _ctx(),
                                   admin=True)
    assert status == 200 and body["crash"]["signature"] == "s3"
    status, body = facade.dispatch("GET", "/v1/tools/debug-runs/..%2Fx", None, _ctx(), admin=True)
    assert status == 404


def test_crash_digest_payload_is_at_most_48kb_and_http_never_confirms():
    facade, service = _facade()
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest",
                                   {"path": "dumps/x.dmp", "engine": "gdb", "wait_seconds": 30},
                                   _ctx(), admin=True)
    assert status == 200 and body["ok"] is True and body["run_id"] == "debug-run-owner"
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 48_000
    assert body["crash"]["truncated"] is True
    _, request, wait, confirmed, source = service.calls[0]
    assert request.engine == "gdb" and request.symbol_server is False
    assert (wait, confirmed, source) == (30, False, "http")


@pytest.mark.parametrize("payload", [
    {},
    {"path": ""},
    {"path": "x.dmp", "engine": "windbg"},
    {"path": "x.dmp", "timeout_seconds": 5},
    {"path": "x.dmp", "wait_seconds": 121},
    {"path": "x.dmp", "symbol_dirs": ["a"] * 9},
    {"path": "x.dmp", "argv": ["gdb", "-ex", "shell id"]},
    {"path": "x" * 1025},
    ["x.dmp"],
])
def test_malformed_crash_requests_are_400(payload):
    facade, service = _facade()
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest", payload, _ctx(), admin=True)
    assert (status, body["error_code"]) == (400, "INVALID_DEBUG_REQUEST")
    assert service.calls == []


def test_triage_profile_and_capture_routes():
    facade, service = _facade()
    status, body = facade.dispatch("POST", "/v1/tools/crash-triage", {"path": "qa/"}, _ctx(),
                                   admin=True)
    assert status == 200 and body["buckets"][0]["count"] == 2
    status, body = facade.dispatch("POST", "/v1/tools/profile-digest",
                                   {"path": "t.json", "top_n": 10}, _ctx(), admin=True)
    assert status == 200 and body["profile"]["label"] == "t.json"
    status, body = facade.dispatch("POST", "/v1/tools/profile-capture-digest",
                                   {"path": "perf.data", "engine": "perf"}, _ctx(), admin=True)
    assert status == 200 and body["status"] == "running"


def test_busy_and_unavailable():
    class Busy(Service):
        def crash(self, *a, **k):
            error = CapacityExceeded("busy")
            error.code = "DEBUG_RUN_BUSY"
            raise error

    facade, _ = _facade(Busy())
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest", {"path": "x"}, _ctx(),
                                   admin=True)
    assert (status, body["error_code"]) == (429, "DEBUG_RUN_BUSY")
    unavailable = DebugToolsHttpFacade(lambda: None, presenters=PRESENTERS, ports=PORTS)
    status, body = unavailable.dispatch("POST", "/v1/tools/crash-digest", {"path": "x"}, _ctx(),
                                        admin=True)
    assert (status, body["error_code"]) == (503, "DEBUG_TOOLS_UNAVAILABLE")


def test_route_table():
    assert http_facade.route_kind("POST", "/v1/tools/crash-digest") == "crash-digest"
    assert http_facade.route_kind("GET", "/v1/tools/crash-digest") is None
    assert http_facade.route_kind("GET", "/v1/tools/debug-runs/r1") == "debug-run"
    assert http_facade.route_kind("POST", "/v1/tools/debug-runs/r1/cancel") == "debug-run-cancel"
    assert http_facade.route_kind("GET", "/v1/tools/inventory") is None


def test_serve_dispatches_debug_routes_on_get_and_post():
    source = (REPO / "sonder_runtime/interfaces/http/serve.py").read_text(encoding="utf-8")
    assert source.count('self._handle_debug_tools_request("GET", path)') == 1
    assert source.count('self._handle_debug_tools_request("POST", _request_route(self.path))') == 1
    assert "_admin_authorized(auth)" in source
    assert "authorize=debug_http_authorizer(service)" in source


class _Refusing:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def __call__(self, tool, arguments, context):
        from sonder_runtime.application.errors import Forbidden

        self.calls.append((tool, dict(arguments)))
        error = Forbidden("refused")
        error.decision = dict(self.decision)
        raise error


@pytest.mark.parametrize("route, tool, payload", [
    ("/v1/tools/crash-digest", "crash_digest", {"path": "x.dmp", "engine": "gdb"}),
    ("/v1/tools/profile-capture-digest", "profile_capture_digest",
     {"path": "perf.data", "engine": "auto", "top_n": 10}),
])
def test_host_launching_routes_are_refused_by_the_permission_decision(route, tool, payload):
    service = Service()
    refusing = _Refusing({"tool": tool, "action": "deny", "call_id": "b41e974681049b97"})
    facade = DebugToolsHttpFacade(lambda: service, presenters=PRESENTERS, ports=PORTS,
                                  authorize=refusing)
    status, body = facade.dispatch("POST", route, payload, _ctx(), admin=True)
    assert (status, body["error_code"]) == (403, "PERMISSION_DENIED")
    assert body["call_id"] == "b41e974681049b97"
    assert service.calls == []
    assert [name for name, _ in refusing.calls] == [tool]
    arguments = refusing.calls[0][1]
    assert arguments["path"] == payload["path"] and arguments["engine"] == payload["engine"]
    assert None not in arguments.values()


def test_a_plan_refused_by_the_host_keeps_its_code():
    service = Service()
    refusing = _Refusing({"tool": "crash_digest", "stage": "plan",
                          "error_code": "ENGINE_UNAVAILABLE"})
    facade = DebugToolsHttpFacade(lambda: service, presenters=PRESENTERS, ports=PORTS,
                                  authorize=refusing)
    status, body = facade.crash_digest({"path": "x.dmp"}, _ctx(), admin=True)
    assert (status, body["error_code"]) == (409, "ENGINE_UNAVAILABLE")
    assert service.calls == []


def test_pure_routes_are_not_graded_as_host_launches():
    service = Service()
    refusing = _Refusing({"tool": "x", "action": "deny"})
    facade = DebugToolsHttpFacade(lambda: service, presenters=PRESENTERS, ports=PORTS,
                                  authorize=refusing)
    assert facade.crash_triage({"path": "x.dmp"}, _ctx(), admin=True)[0] == 200
    assert facade.profile_digest({"path": "t.json"}, _ctx(), admin=True)[0] == 200
    assert refusing.calls == []


def _serve_as(monkeypatch, *, authorized, role, username="alice"):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": authorized, "mode": "account",
        "account": {"role": role, "username": username}, "api_key": False})


def _serve_app(monkeypatch, service):
    """Serve ``service``; the host-launch permission decision is recorded and allowed.

    The real decision (``debug_http_authorizer``) is exercised against the
    real service in tests/test_debug_permissions.py.
    """
    graded = []

    def authorizer(bound):
        assert bound is service

        def authorize(tool, arguments, context):
            graded.append((tool, dict(arguments), context.source, len(service.calls)))
            return "permission:test"

        return authorize

    monkeypatch.setattr(DebugToolsHttpFacade, "_modules", lambda self: (PRESENTERS, PORTS))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: SimpleNamespace(debug_tools=service))
    monkeypatch.setattr("sonder_runtime.bootstrap.debug_tools.debug_http_authorizer", authorizer)
    return graded


def test_serve_non_admin_gets_403_before_the_app_is_built(http_server, monkeypatch):
    _serve_as(monkeypatch, authorized=True, role="user")
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: pytest.fail("denied debug request constructed the app"))
    status, body = _post(http_server, "/v1/tools/crash-digest", {"path": "x.dmp"})
    assert (status, body["error_code"]) == (403, "FORBIDDEN")
    assert _get(http_server, "/v1/tools/debug-runs/debug-run-owner")[0] == 403


def test_serve_admin_symbol_server_is_403_and_foreign_runs_404(http_server, monkeypatch):
    service = Service()
    _serve_as(monkeypatch, authorized=True, role="admin")
    graded = _serve_app(monkeypatch, service)
    status, body = _post(http_server, "/v1/tools/crash-digest",
                         {"path": "x.dmp", "symbol_server": True})
    assert (status, body["error_code"]) == (403, "SYMBOL_SERVER_NEEDS_CONSOLE")
    assert graded == []
    status, body = _get(http_server, "/v1/tools/debug-runs/debug-run-owner")
    assert (status, body["error_code"]) == (404, "JOB_NOT_FOUND")
    status, body = _post(http_server, "/v1/tools/crash-digest", {"path": "x.dmp"})
    assert status == 200 and body["run_id"] == "debug-run-owner"
    assert service.calls[-1][-1] == "http"
    # The permission decision ran first, on the typed arguments, as http.
    assert len(graded) == 1
    tool, arguments, source, calls_before = graded[0]
    assert (tool, source) == ("crash_digest", "http")
    assert arguments["path"] == "x.dmp" and arguments["symbol_server"] is False
    assert calls_before == len(service.calls) - 1
    assert _post(http_server, "/v1/tools/crash-digest", {"path": "x", "argv": ["sh"]})[0] == 400


# --- architecture ------------------------------------------------------------------------------


@pytest.mark.parametrize("relative", [
    "sonder_runtime/interfaces/repl/facades/debug_tools.py",
    "sonder_runtime/interfaces/http/facades/debug_tools.py",
])
def test_interfaces_import_no_domain_module(relative):
    tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "domain" not in module.split("."), (relative, module)
            assert not module.startswith(("sonder_runtime.adapters", "sonder_runtime.bootstrap"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert ".domain" not in alias.name, (relative, alias.name)


@pytest.mark.parametrize("field,value", [
    ("path", "x.dmp\n-ex shell id"), ("path", "a\x1b[2J.dmp"), ("executable", "g\x00ame"),
    ("symbol_dirs", ["ok", "C:\\syms\r\n.load evil"]), ("path", "a\u202e.dmp"),
])
def test_control_characters_in_paths_are_refused_before_the_service(field, value):
    facade, service = _facade()
    payload = {"path": "x.dmp", field: value}
    status, body = facade.dispatch("POST", "/v1/tools/crash-digest", payload, _ctx(), admin=True)
    assert (status, body["error_code"]) == (400, "INVALID_DEBUG_REQUEST")
    assert service.calls == []
