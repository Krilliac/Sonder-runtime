import pytest
from sonder_runtime.interfaces.http.app_control import _route


@pytest.mark.parametrize(
    "method,path,action,payload",
    [
        ("POST", "/v1/app-control/work", "prepare_work", {}),
        (
            "POST",
            "/v1/app-control/work/" + "a" * 64 + "/execute",
            "execute_work",
            {"work_id": "a" * 64},
        ),
        ("GET", "/v1/app-control/work/" + "a" * 64, "read_work", {"work_id": "a" * 64}),
    ],
)
def test_exact_managed_work_routes(method, path, action, payload):
    assert _route(method, path) == (action, payload)


def test_managed_work_composition_requires_owned_registration():
    from sonder_runtime.bootstrap.app_managed_work_http import AppManagedWorkHttpBinding

    with pytest.raises((PermissionError, TypeError)):
        AppManagedWorkHttpBinding(
            None,
            application=None,
            runtime=None,
            permission_engine=None,
            register_owned=None,
            require_owned=None,
        )


import json
import http.client
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import admin_auth
import permission_modes
import server
from tests.test_app_control_http import control, invoke
from tests.test_tier_escalation import _install_agent_fakes


@pytest.fixture
def work_http(control, monkeypatch):
    """Real HTTP/DB/dispatcher with an explicit test-owned registration slot.

    The foreground ManagedRuntimeOwner process proof is a separate dependency.
    This harness makes no claim to that proof or a running production profile.
    """
    from http.server import ThreadingHTTPServer
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
    from sonder_runtime.application.ports.model_gateway import ModelResponse
    from sonder_runtime.bootstrap.app_managed_work_http import AppManagedWorkHttpBinding

    control_binding, token, state, account_open, catalog, entry = control
    private = Path(control_binding.store.path).parent
    sessions = SQLiteSessionRepository(private / "canonical-sessions.db")
    lanes = AgentLaneService(
        SQLiteAgentLaneStore(control_binding.store.path, sessions),
        sessions,
        SimpleNamespace(
            generate=lambda *a: ModelResponse(
                "Completed", "scripted", "code", tokens_out=1
            )
        ),
        auto_start=False,
        allowed_tools=("read_file",),
    )
    models = _install_agent_fakes(
        monkeypatch, {"m-code": '{"final":"inspected repository"}'}
    )
    app = replace(
        server._application(), config=state["config"], agent_lanes=lambda: lanes
    )
    monkeypatch.setattr(server, "_application", lambda: app)
    ledger = ApprovalLedger(private / "approvals.db")
    engine = SimpleNamespace(
        approval_ledger=lambda: ledger,
        call_digest=permission_modes.call_digest,
        decide=lambda tool, **kw: permission_modes.decide(
            tool, mode="manual", rule_lookup=lambda _: None, **kw
        ),
    )
    slot = []

    def register(application, dispatcher):
        assert application is app and dispatcher.application is app and not slot
        slot.append(dispatcher)
        return SimpleNamespace(commit=lambda: None, rollback=lambda **kw: dispatcher.close())

    def require(application):
        if application is not app or not slot:
            raise PermissionError("test owned slot unavailable")
        return slot[0]

    service = AppManagedWorkHttpBinding(
        control_binding,
        application=app,
        runtime=server,
        permission_engine=engine,
        register_owned=register,
        require_owned=require,
        output_root=private / "output",
    )
    failures = []
    original_execute = service.dispatcher.workbench.execute_prepared_workbench

    def checked_execute(*args, **kwargs):
        try:
            return original_execute(*args, **kwargs)
        except BaseException:
            import traceback

            failures.append(traceback.format_exc())
            raise

    monkeypatch.setattr(
        service.dispatcher.workbench, "execute_prepared_workbench", checked_execute
    )
    control_binding._work_binding = service
    enrolled = invoke(
        control_binding,
        token,
        "enroll",
        dict(command_id="work-enroll", project="project1", password="test-password"),
    )[1]
    credential = enrolled["control_token"]
    created = invoke(
        control_binding,
        token,
        "create_binding",
        dict(command_id="work-create"),
        credential,
    )[1]
    bid = created["receipt"]["entity_id"]
    assert (
        invoke(
            control_binding,
            token,
            "select_binding",
            dict(
                command_id="work-select",
                binding_id=bid,
                expected_binding_revision=1,
                expected_epoch=0,
            ),
            credential,
        )[0]
        == 200
    )
    monkeypatch.setattr(serve, "_APP_CONTROL_BINDING", control_binding)
    monkeypatch.setattr(serve, "API_KEY", "deployment-key")
    monkeypatch.setattr(serve, "AUTH_MODE", "both")
    listener = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": token,
        "X-Sonder-App-Control": credential,
    }

    def request(method, path, body=None, *, custom_headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", listener.server_port, timeout=120
        )
        conn.request(
            method,
            path,
            None if body is None else json.dumps(body).encode(),
            headers if custom_headers is None else custom_headers,
        )
        response = conn.getresponse()
        result = (
            response.status,
            dict(response.getheaders()),
            json.loads(response.read()),
        )
        conn.close()
        return result

    yield SimpleNamespace(
        service=service,
        request=request,
        models=models,
        ledger=ledger,
        headers=headers,
        token=token,
        credential=credential,
        control=control_binding,
        account_open=account_open,
        catalog=catalog,
        entry=entry,
        port=listener.server_port,
        slot=slot,
        failures=failures,
    )
    listener.shutdown()
    listener.server_close()
    thread.join(timeout=5)
    service.dispatcher.close()
    slot.clear()
    lanes.close()


def preparation():
    return dict(
        command_id="prepare1",
        prompt="Inspect the repository",
        tier="code",
        max_steps=1,
        allow_web=False,
    )


def test_real_http_prepare_ask_approve_execute_status_and_exact_retry(work_http):
    h = work_http
    status, headers, prepared = h.request("POST", "/v1/app-control/work", preparation())
    assert status == 200, prepared
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert prepared["receipt"]["action"] == "prepare_work"
    assert prepared["receipt"]["result_code"] == "COMMITTED"
    assert not h.models
    assert h.request("POST", "/v1/app-control/work", preparation())[2] == prepared
    wid = prepared["work"]["work_id"]
    path = "/v1/app-control/work/" + wid
    status, _, pending = h.request("POST", path + "/execute", {})
    assert (
        status == 409 and pending["error"]["code"] == "APP_WORK_APPROVAL_PENDING"
    ), pending
    assert not h.models
    call = h.ledger.resolve_call(pending["pending"]["call_digest"])
    issued = h.ledger.issue(call.tool, call.digest, approver="operator", surface="repl")
    status, _, accepted = h.request("POST", path + "/execute", {})
    assert status == 202, accepted
    h.service.dispatcher._executor.shutdown(wait=True)
    status, _, completed = h.request("GET", path)
    assert status == 200, completed
    assert not h.failures, h.failures
    assert completed["work"]["state"] == "terminal", completed
    assert completed["work"]["completion"] == {"phase": "not_required"}
    assert h.models == ["m-code"] and h.ledger.get(issued.nonce).spent
    assert h.request("POST", path + "/execute", {})[2] == completed
    assert h.models == ["m-code"]
    encoded = json.dumps(completed)
    for private in (
        h.token,
        h.credential,
        h.entry["roots"][0],
        "account-session-v1:",
        "Inspect the repository",
        "m-code",
        "approval_nonce",
    ):
        assert private not in encoded
    assert not h.service.authority._selections


@pytest.mark.parametrize(
    "change", ["api", "account", "control", "origin", "body_scope"]
)
def test_work_http_refuses_untrusted_scope_before_preparation(work_http, change):
    h = work_http
    headers = dict(h.headers)
    body = preparation()
    if change in ("api", "account", "control"):
        headers.pop(
            {
                "api": "Authorization",
                "account": "X-Sonder-Account-Token",
                "control": "X-Sonder-App-Control",
            }[change]
        )
    elif change == "origin":
        headers["Origin"] = "https://foreign.invalid"
    else:
        body["workspace_root"] = h.entry["roots"][0]
    status, response_headers, body = h.request(
        "POST", "/v1/app-control/work", body, custom_headers=headers
    )
    assert status in (400, 401, 403), body
    assert response_headers["Cache-Control"] == "no-store"
    assert not h.models
    assert not h.service.authority._selections


def test_work_http_unregistered_slot_never_lazily_creates_dispatcher(work_http):
    h = work_http
    held = h.slot.pop()
    try:
        status, _, body = h.request("POST", "/v1/app-control/work", preparation())
        assert status == 503 and not h.models, body
    finally:
        h.slot.append(held)


@pytest.mark.parametrize(
    "change", ["logout", "role", "rotation", "catalog", "selection"]
)
def test_current_auth_publication_refuses_after_preparation_commit(
    work_http, monkeypatch, change
):
    h = work_http
    original = h.service.dispatcher.prepare
    committed = []

    def prepare_then_change(*args, **kwargs):
        row = original(*args, **kwargs)
        committed.append(row)
        if change in ("logout", "role"):
            conn = h.account_open()
            try:
                if change == "logout":
                    admin_auth.revoke_session(conn, h.token)
                else:
                    admin_auth.set_account(conn, "alice", role="user")
            finally:
                conn.close()
        elif change == "rotation":
            monkeypatch.setenv("SONDER_AUTH_SECRET", "replacement-secret-" + "b" * 48)
        elif change == "catalog":
            h.entry["revision"] = 2
            h.catalog.write_text(
                json.dumps(dict(version=1, grants=[h.entry])), encoding="utf8"
            )
        else:
            assert (
                invoke(
                    h.control,
                    h.token,
                    "clear_selection",
                    dict(command_id="clear-before-publish", expected_epoch=1),
                    h.credential,
                )[0]
                == 200
            )
        return row

    monkeypatch.setattr(h.service.dispatcher, "prepare", prepare_then_change)
    status, headers, body = h.request("POST", "/v1/app-control/work", preparation())
    assert status >= 400 and "work" not in body and "receipt" not in body, body
    assert headers["Cache-Control"] == "no-store"
    assert len(committed) == 1 and not h.models
    assert not h.service.authority._selections


def test_publication_failure_is_not_a_second_response_or_second_prepare(work_http):
    h = work_http
    calls = []

    def fail(status, body):
        calls.append((status, body))
        raise ConnectionError("disposable disconnected client")

    with pytest.raises(ConnectionError):
        h.service.perform(
            "prepare_work",
            preparation(),
            account_token=h.token,
            control_token=h.credential,
            publish=fail,
        )
    assert len(calls) == 1 and calls[0][0] == 200
    assert not h.service.authority._selections and not h.models
    status, _, retained = h.request("POST", "/v1/app-control/work", preparation())
    assert status == 200 and retained == calls[0][1]


def test_other_account_cannot_read_exact_work_or_use_control_credential(work_http):
    h = work_http
    first = h.request("POST", "/v1/app-control/work", preparation())[2]
    conn = h.account_open()
    try:
        admin_auth.register(conn, "bob", "different-password", role="admin")
        token, _ = admin_auth.login(conn, "bob", "different-password")
    finally:
        conn.close()
    headers = dict(h.headers, **{"X-Sonder-Account-Token": token})
    status, _, body = h.request(
        "GET",
        "/v1/app-control/work/" + first["work"]["work_id"],
        custom_headers=headers,
    )
    assert status == 401 and "work" not in body
    assert not h.models


@pytest.mark.parametrize("kind", ["duplicate_account", "oversize", "execute_body"])
def test_work_route_rejects_ambiguous_or_unbounded_wire_requests(work_http, kind):
    h = work_http
    conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=15)
    path = "/v1/app-control/work"
    raw = json.dumps(preparation()).encode()
    if kind == "oversize":
        raw = b" " * 16385
    if kind == "execute_body":
        path += "/" + "a" * 64 + "/execute"
        raw = b'{"work_id":"spoof"}'
    conn.putrequest("POST", path)
    for key, value in h.headers.items():
        conn.putheader(key, value)
    if kind == "duplicate_account":
        conn.putheader("X-Sonder-Account-Token", h.token)
    conn.putheader("Content-Length", str(len(raw)))
    conn.endheaders(raw)
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    assert response.status in (400, 413), body
    assert "work" not in body and not h.models
    assert not h.service.authority._selections


def test_request_account_slots_are_bounded_and_removed(work_http):
    h = work_http
    from sonder_runtime.bootstrap.app_control_http import _principal

    conn = h.account_open()
    try:
        account = h.control._account(conn, h.token)
    finally:
        conn.close()
    key = _principal(account)
    h.service._account_active[key] = 2
    try:
        status, _, body = h.request("POST", "/v1/app-control/work", preparation())
        assert status == 429 and body["error"]["code"] == "APP_WORK_BUSY"
        assert h.service._account_active[key] == 2
    finally:
        h.service._account_active.clear()
    assert not h.service.authority._selections and not h.models
