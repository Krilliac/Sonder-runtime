"""Regression tests for permission modes, effect fences, slash args, and HTTP auth/replication hardening."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
from email.message import Message
from types import SimpleNamespace

import pytest

import permission_modes as pm
import sonder_runtime.interfaces.http.serve as serve
from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.security import approval_ledger, role_tool_policy
from sonder_runtime.adapters.security.permission_policy import permission_policy
from sonder_runtime.application.execution import resume_reality
from sonder_runtime.application.memory.replication import MemoryReplicationReceiver
from sonder_runtime.domain.memory.replication import MemoryReplicationError
from sonder_runtime.interfaces.http import memory_replication
from sonder_runtime.interfaces.http.facades import approval_routes, developer_chat

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_gate(monkeypatch, tmp_path):
    home = tmp_path / "sonder-home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_STATE_HOME", str(home))
    pm._reset_for_tests()
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    pm.reset_unattended_for_tests()
    yield
    pm._reset_for_tests()
    pm.reset_unattended_for_tests()


# --- 1. Effect-fence completeness ---------------------------------------------


def test_effect_fence_check_all_catches_lost_outer_fence_beneath_holding_inner(isolated_gate):
    outer_lost = effect_fence.Fence("fleet:outer", lambda: "outer fleet lease lost")
    inner_ok = effect_fence.Fence("autopilot:inner", lambda: "")

    with effect_fence.held(outer_lost), effect_fence.held(inner_ok):
        assert effect_fence.current() is inner_ok
        assert "outer fleet lease lost" in effect_fence.check_all()
        # Default fence=None consults check_all() across the entire stack.
        d_default = pm.decide("file_write", interactive=False)
        assert d_default.action == pm.DENY and d_default.source == "fence"
        assert "outer fleet lease lost" in d_default.reason
        # Passing fence=effect_fence.current() also checks the full stack.
        d_current = pm.decide("file_write", interactive=False, fence=effect_fence.current())
        assert d_current.action == pm.DENY and d_current.source == "fence"
        # Reads remain unfenced.
        assert pm.decide("file_read", interactive=False).action == pm.ALLOW
        # Explicit distinct test override still takes precedence.
        override_ok = effect_fence.Fence("override", lambda: "")
        assert pm.decide("file_write", interactive=False, fence=override_ok).action == pm.ALLOW


def test_consume_preapproved_decision_never_bypasses_effect_fence_or_resume_reality(isolated_gate):
    allow_write = pm.Decision(pm.ALLOW, pm.AUTO, "mutation", "preapproved", "file_write")
    allow_read = pm.Decision(pm.ALLOW, pm.AUTO, "safe", "preapproved", "file_read")
    lost = effect_fence.Fence("fleet:a1", lambda: "cancelled")

    with pm.preapproved_scope("file_write", allow_write):
        with effect_fence.held(lost):
            assert pm.consume_preapproved_decision("file_write") is None

    # Reads are not fenced and still consume preapproved decisions.
    with pm.preapproved_scope("file_read", allow_read):
        with effect_fence.held(lost):
            assert pm.consume_preapproved_decision("file_read") is allow_read

    # Resume-reality mutation block also stops preapproved effect consumption.
    barrier = resume_reality.ResumeBarrier({"requires_reinspection": True})
    with pm.preapproved_scope("file_write", allow_write):
        with resume_reality.bound(barrier):
            assert pm.consume_preapproved_decision("file_write") is None


# --- 2. Permission mode & risk-grading fixes ----------------------------------


def test_untrimmed_tool_name_cannot_bypass_durable_authority_or_bypass_held(isolated_gate):
    assert permission_policy.is_durable_authority_tool(" admin_register ") is True
    assert permission_policy.is_durable_authority_tool(" /permission_approve ") is True

    for raw_name in (" admin_register", "  permission_approve  ", " /admin_register"):
        d_bypass = pm.decide(raw_name, interactive=False, mode=pm.BYPASS)
        assert d_bypass.action == pm.DENY
        assert d_bypass.source == "durable-authority"
        d_auto = pm.decide(raw_name, interactive=False, mode=pm.AUTO)
        assert d_auto.action == pm.DENY
        assert d_auto.source == "durable-authority"


def test_credential_arguments_strip_parent_token_and_password():
    assert "parent_token" in pm.CREDENTIAL_ARGUMENTS
    assert "password" in pm.CREDENTIAL_ARGUMENTS
    a = pm.call_digest("agent_lane", {"action": "status", "parent_token": "secret-1", "password": "pw1"})
    b = pm.call_digest("agent_lane", {"action": "status", "parent_token": "secret-2", "password": "pw2"})
    assert a and a == b
    keys = {k for k, _v, _lit in pm.preview_fields({"action": "status", "parent_token": "s", "password": "p"})}
    assert keys == {"action"}


def test_ensemble_codegen_and_workflow_risk_grades(isolated_gate):
    assert pm.risk_of("ensemble_codegen_build_loop") == "execution"
    assert pm.decide("ensemble_codegen_build_loop", interactive=False, mode=pm.ACCEPT_EDITS).action == pm.DENY

    assert pm.risk_of("workflow_save") == "mutation"
    assert pm.risk_of("workflow_delete") == "mutation"
    assert pm.decide("workflow_save", interactive=False, mode=pm.MANUAL).action == pm.DENY
    assert pm.decide("workflow_delete", interactive=False, mode=pm.MANUAL).action == pm.DENY


def test_mode_scope_enter_resets_contextvars_if_owner_scope_raises(isolated_gate, monkeypatch):
    def boom(_owner):
        raise RuntimeError("owner_scope boom")

    monkeypatch.setattr(approval_ledger, "owner_scope", boom)
    before_scope = pm._MODE_SCOPE.get()
    before_capped = pm._MODE_SCOPE_CAPPED.get()

    with pytest.raises(RuntimeError, match="owner_scope boom"):
        with pm.mode_scope("account:alice", "sess-1", local_open=True):
            pass

    assert pm._MODE_SCOPE.get() == before_scope
    assert pm._MODE_SCOPE_CAPPED.get() == before_capped


# --- 3. Slash argument parsing for /runprogram, /runscript, /run --------------


def test_slash_call_arguments_parses_runprogram_runscript_and_run(isolated_gate):
    prog_args = developer_chat.slash_call_arguments("/runprogram", 'cmd | ["/c", "del", "foo.txt"] | src')
    assert prog_args == {"program": "cmd", "args_json": '["/c", "del", "foo.txt"]', "cwd": "src"}
    assert pm.always_asks("workspace_run", prog_args) is True
    assert pm.decide("workspace_run", interactive=False, mode=pm.AUTO, arguments=prog_args).action == pm.DENY
    assert pm.decide("workspace_run", interactive=False, mode=pm.BYPASS, arguments=prog_args).action == pm.DENY

    ps_args = developer_chat.slash_call_arguments("/runprogram", 'powershell | ["-enc", "UwB0AGEAcgB0AA=="]')
    assert ps_args == {"program": "powershell", "args_json": '["-enc", "UwB0AGEAcgB0AA=="]', "cwd": "."}
    assert pm.decide("workspace_run", interactive=False, mode=pm.BYPASS, arguments=ps_args).action == pm.DENY

    script_args = developer_chat.slash_call_arguments("/runscript", "evil.ps1 | []")
    assert script_args == {"path": "evil.ps1", "args_json": "[]", "cwd": ""}
    # Unresolvable/opaque .ps1 script fails closed even in auto mode.
    assert pm.decide("script_run", interactive=False, mode=pm.AUTO, arguments=script_args).action == pm.DENY

    py_delete = developer_chat.slash_call_arguments("/run", "import os; os.remove('victim.txt')")
    assert py_delete == {"code": "import os; os.remove('victim.txt')", "language": "python"}
    assert pm.decide("run_code", interactive=False, mode=pm.BYPASS, arguments=py_delete).action == pm.DENY

    fenced_bash = developer_chat.slash_call_arguments("/run", "```bash\nrm -rf /tmp/x\n```")
    assert fenced_bash == {"code": "rm -rf /tmp/x", "language": "bash"}
    assert pm.decide("run_code", interactive=False, mode=pm.BYPASS, arguments=fenced_bash).action == pm.DENY

    fenced_ps = developer_chat.slash_call_arguments("/run", "```powershell\nInvoke-Expression $x\n```")
    assert fenced_ps == {"code": "Invoke-Expression $x", "language": "powershell"}
    assert pm.decide("run_code", interactive=False, mode=pm.AUTO, arguments=fenced_ps).action == pm.DENY


# --- 4. HTTP auth & scoping fixes ---------------------------------------------


def test_assigned_api_key_does_not_inherit_account_bypass_on_non_chat_routes(isolated_gate, monkeypatch):
    # Account alice enters bypass in sess-1.
    with pm.mode_scope("account:alice", "sess-1"):
        with pm.attended_mode_change(), pm.attended_bypass_confirm():
            pm.set_mode(pm.BYPASS)
        assert pm.current_mode() == pm.BYPASS

    # Non-chat request authenticated via alice's assigned sk- API key.
    api_key_ctx = {
        "authorized": True,
        "mode": "account",
        "api_key": False,
        "account": {"username": "alice", "role": "admin", "via_api_key": True},
    }
    monkeypatch.setattr(serve, "_auth_context", lambda _a, _t: api_key_ctx)

    handler = serve.Handler.__new__(serve.Handler)
    handler.headers = Message()
    handler.headers["X-Sonder-Session"] = "sess-1"
    handler._mode_scope_bound = False

    with contextlib.ExitStack() as stack:
        handler._turn_stack = stack
        ctx = handler._request_auth_context()
        assert ctx is api_key_ctx
        # Must NOT inherit account:alice's bypass mode!
        assert pm.current_mode() != pm.BYPASS
        # Approval ledger owner scope still belongs to account:alice.
        assert approval_ledger.current_owner().startswith("ap-")

    # After ExitStack unwinds, keep-alive ContextVars are cleanly unbound (not bound to unrestricted None).
    assert role_tool_policy._CALLER_ROLE.get(role_tool_policy._UNBOUND) is role_tool_policy._UNBOUND
    assert role_tool_policy.caller_bound_unrestricted() is False
    assert pm._MODE_SCOPE_CAPPED.get() is False


def test_chat_completion_and_stream_do_not_fall_back_to_global_latest_activity(monkeypatch):
    monkeypatch.setattr(
        serve.server.activity_tracker,
        "public_snapshot",
        lambda **_kwargs: {"latest": {"id": "r_other_tenant"}},
    )
    obj = serve._chat_completion_object("hello", "sonder", activity_response=None)
    assert obj["sonder_activity"] is None

    # Verify _send_stream also sends None instead of r_other_tenant.
    handler = serve.Handler.__new__(serve.Handler)
    handler.headers = Message()
    handler._early_stream = None
    handler.connection = None
    handler._request_started = 0.0
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _k, _v: None
    handler.end_headers = lambda: None
    handler._cors = lambda: None
    assert handler._send_stream("hello", "sonder", activity_response=None) is True
    raw = handler.wfile.getvalue().decode("utf-8")
    assert "r_other_tenant" not in raw


def test_duplicate_origin_rejected_and_local_server_log_suppresses_cors(monkeypatch):
    monkeypatch.setattr(serve, "CORS_ORIGINS", frozenset({"http://allowed.example"}))

    # Duplicate Origin headers -> rejected in _reject_disallowed_origin and not reflected in _cors.
    handler = serve.Handler.__new__(serve.Handler)
    handler.command = "GET"
    handler.path = "/v1/sonder/capabilities"
    handler.headers = Message()
    handler.headers.add_header("Origin", "http://allowed.example")
    handler.headers.add_header("Origin", "http://evil.example")
    sent_payloads = []
    sent_headers = []
    handler._send_json_payload = lambda payload, status=200, **_kw: sent_payloads.append((status, payload))
    handler.send_header = lambda k, v: sent_headers.append((k, v))

    assert handler._reject_disallowed_origin() is True
    assert sent_payloads and sent_payloads[0][0] == 403
    assert sent_payloads[0][1]["error"]["code"] == "forbidden_origin"
    handler._cors()
    assert not any(k.lower() == "access-control-allow-origin" for k, _ in sent_headers)

    # /v1/local/server-log suppresses CORS even for an allowed Origin.
    log_handler = serve.Handler.__new__(serve.Handler)
    log_handler.command = "GET"
    log_handler.path = "/v1/local/server-log"
    log_handler.headers = Message()
    log_handler.headers["Origin"] = "http://allowed.example"
    log_headers = []
    log_handler.send_header = lambda k, v: log_headers.append((k, v))
    log_handler._cors()
    assert not any(k.lower() == "access-control-allow-origin" for k, _ in log_headers)


def test_capabilities_charges_auth_failure_only_when_credentials_supplied(monkeypatch):
    failures = []

    class _Lifecycle:
        def auth_attempt_allowed(self, _ip):
            return True

        def record_auth_failure(self, ip, reason):
            failures.append((ip, reason))

    monkeypatch.setattr(serve, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(serve, "API_KEY", "k" * 40)
    monkeypatch.setattr(serve, "AUTH_MODE", "api-key")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve.sonder_lifecycle, "get", lambda: _Lifecycle())

    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        # Probe without auth headers: no failure charged.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/sonder/capabilities")
        assert conn.getresponse().status == 200
        conn.close()
        assert failures == []

        # Probe with wrong Authorization header: failure charged!
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/sonder/capabilities", headers={"Authorization": "Bearer wrong"})
        assert conn.getresponse().status == 200
        conn.close()
        assert len(failures) == 1
        assert failures[0][1] == "invalid-credentials"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_handle_commands_get_calls_request_auth_context_once(monkeypatch):
    calls = []
    handler = serve.Handler.__new__(serve.Handler)
    handler.path = "/v1/commands"
    handler.headers = Message()
    handler._request_auth_context = lambda: (calls.append(1) or {"authorized": True, "mode": "local-open", "account": None})
    sent = []
    handler._send_json_payload = lambda payload, status=200, **_kw: sent.append((status, payload))
    monkeypatch.setattr(serve, "_commands_index_payload", lambda context=None: {"commands": []})

    assert handler._handle_commands_get() is True
    assert len(calls) == 1
    assert sent == [(200, {"commands": []})]


def test_approval_routes_has_single_canonical_helpers():
    assert callable(approval_routes._decoded_segments)
    assert approval_routes._decoded_segments("a%40b/c", approval_routes._default_unquote) == ["a@b", "c"]
    assert approval_routes._approval_role({"account": {"role": "admin", "via_api_key": True}}) == "developer"


# --- 5. Pre-auth DoS & framing on POST /v1/memory/replication/batches ---------


class _DummySink:
    identity = "node-b"

    def apply(self, _batch):
        raise AssertionError("sink should not be called")


class _TrackingStream:
    def __init__(self, data: bytes):
        self._data = data
        self.read_calls = 0

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        return self._data[:n] if n >= 0 else self._data


class _RepHandler:
    def __init__(self, body: bytes, *, authorization: str | None = "Bearer secret-key",
                 content_length: str | None = None, rate_limited: bool = False):
        self.path = memory_replication.MEMORY_REPLICATION_ROUTE
        self.headers = Message()
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body)) if content_length is None else content_length
        self.rfile = _TrackingStream(body)
        self.responses = []
        self._request_body_consumed = False
        self._rate_limited = rate_limited
        self.auth_failures = []

    def _auth_rate_limited(self):
        if self._rate_limited:
            self.responses.append((429, {"error": {"code": "RATE_LIMITED"}}, {}))
            return True
        return False

    def _record_auth_failure(self, reason="invalid-credentials"):
        self.auth_failures.append(reason)

    def _send_json_payload(self, payload, *, status, headers):
        self.responses.append((status, payload, headers))


def _receiver() -> MemoryReplicationReceiver:
    return MemoryReplicationReceiver(
        _DummySink(),
        api_key="secret-key",
        accepted_source_ids=("node-a",),
    )


def test_memory_replication_rejects_bad_auth_before_reading_body_or_json(monkeypatch):
    receiver = _receiver()
    handler = _RepHandler(b"{bad json", authorization="Bearer wrong")
    monkeypatch.setattr(
        "sonder_runtime.application.memory.replication.json.loads",
        lambda *_a, **_k: pytest.fail("json.loads must not run pre-auth"),
    )

    assert memory_replication.handle_memory_replication(handler, "POST", receiver) is True
    assert handler.responses[0][0] == 401
    assert handler.rfile.read_calls == 0
    assert handler._request_body_consumed is False
    assert handler.auth_failures == ["invalid-credentials"]

    # Direct receive_bytes also checks auth before json.loads.
    with pytest.raises(PermissionError):
        receiver.receive_bytes("Bearer wrong", b"{not-json")


def test_memory_replication_rejects_transfer_encoding_and_rate_limit_and_busy_pre_body():
    receiver = _receiver()

    # Transfer-Encoding rejected with 400 before reading body.
    te_handler = _RepHandler(b"{}")
    te_handler.headers["Transfer-Encoding"] = "chunked"
    assert memory_replication.handle_memory_replication(te_handler, "POST", receiver) is True
    assert te_handler.responses[0][0] == 400
    assert te_handler.rfile.read_calls == 0

    # Rate-limited client rejected before reading body.
    rl_handler = _RepHandler(b"{}", rate_limited=True)
    assert memory_replication.handle_memory_replication(rl_handler, "POST", receiver) is True
    assert rl_handler.responses[0][0] == 429
    assert rl_handler.rfile.read_calls == 0

    # Exhausted concurrency semaphore returns 429 MEMORY_REPLICATION_BUSY before reading body.
    acquired = 0
    try:
        while memory_replication._CONCURRENCY_SLOTS.acquire(blocking=False):
            acquired += 1
        busy_handler = _RepHandler(b"{}")
        assert memory_replication.handle_memory_replication(busy_handler, "POST", receiver) is True
        assert busy_handler.responses[0][0] == 429
        assert busy_handler.responses[0][1]["error"]["code"] == "MEMORY_REPLICATION_BUSY"
        assert busy_handler.rfile.read_calls == 0
    finally:
        for _ in range(acquired):
            memory_replication._CONCURRENCY_SLOTS.release()


def test_memory_replication_deep_json_returns_400_and_short_read_keeps_unconsumed():
    receiver = _receiver()

    # Deeply nested JSON does not raise RecursionError / 503.
    deep_body = (b"[" * 2000) + (b"]" * 2000)
    deep_handler = _RepHandler(deep_body)
    assert memory_replication.handle_memory_replication(deep_handler, "POST", receiver) is True
    assert deep_handler.responses[0][0] == 400
    assert deep_handler.responses[0][1]["error"]["code"] == "INVALID_REQUEST"

    # Also verify RecursionError from json.loads directly maps to MemoryReplicationError.
    with pytest.raises(MemoryReplicationError):
        receiver.receive_bytes("Bearer secret-key", deep_body)

    # Short read leaves _request_body_consumed = False so connection is closed.
    short_handler = _RepHandler(b"abc", content_length="10")
    assert memory_replication.handle_memory_replication(short_handler, "POST", receiver) is True
    assert short_handler.responses[0][0] == 400
    assert short_handler._request_body_consumed is False
