"""Tests for ChatToolTurn native tool calls, mid-turn SSE approval waits, and bidi detection."""
from __future__ import annotations

import json
import threading
import time

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.chat_run_backend import ChatRunBackend
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.chat.honesty import guard_unverified_run_claim
from sonder_runtime.application.chat.tool_loop import (
    ChatToolTurn,
    bind_turn,
    build_tool_turn_manifest,
)
from sonder_runtime.domain.chat_tools import (
    ChatToolBudget,
    chat_tools_payload,
    detect_bidi_chars,
)
from sonder_runtime.interfaces.http import chat_tool_stream
from sonder_runtime.interfaces.http.facades import approvals as approvals_facade
from sonder_runtime.interfaces.http.facades import developer_chat
from tests.test_http_approvals import API_KEY, _http_server, _request, _serve_with_accounts

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_permission_state():
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def test_detect_bidi_chars_and_pending_listing_flags_rlo(tmp_path, monkeypatch):
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    code = "print('\u202ehi')"
    has_bidi, chars = detect_bidi_chars(code)
    assert has_bidi and "U+202E" in chars

    decision = pm.decide("run_code", interactive=False, mode=pm.MANUAL, surface="http", arguments={"code": code})
    assert decision.action == pm.DENY and decision.call_id
    pending = ledger.resolve_call(decision.call_id)
    view = approvals_facade.pending_view(pending, show_detail=True)
    assert view["bidi_warning"] is True
    assert "U+202E" in view["bidi_chars"]


def test_chat_tool_turn_budget_and_honesty_guard():
    executed = []
    manifest = build_tool_turn_manifest(admitted_tools=["run_code", "tool_search"], user_run_command=True, mode="auto")
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_kw: executed.append((name, args)) or "6",
        catalog_provider=lambda: {"file_read": {"description": "Read a file.", "input_schema": {}}},
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    events = []
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code", "tool_search"]),
        backend=backend,
        permission_policy=pm,
        budget=ChatToolBudget(max_runs=1, max_model_calls=4, model_output_chars=50),
        on_tool_event=events.append,
    )

    replies = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "run_code", "arguments": {"code": "print(2*3)"}}},
            {"function": {"name": "run_code", "arguments": {"code": "print(999)"}}},
        ]}},
        {"message": {"role": "assistant", "content": "The answer is 6."}},
    ])
    old_mode = pm.current_mode()
    pm.set_mode(pm.AUTO)
    try:
        outcome = turn.run(lambda _msgs, _tools: next(replies), system="sys", prompt="compute", history=None)
    finally:
        pm.set_mode(old_mode)

    assert "6" in outcome.content
    assert len(executed) == 1
    assert [r.status for r in outcome.records] == ["ok", "budget_exceeded"]
    assert any(e["phase"] == "started" for e in events)
    assert any(e["phase"] == "finished" for e in events)

    # Honesty check: claiming execution without a successful run appends disclosure.
    unverified = guard_unverified_run_claim("I just ran the code and all 3 tests passed!", [])
    assert "no code or test tool executed" in unverified.lower()


def test_mid_turn_approval_wait_wakes_on_http_approve_and_deny(tmp_path, monkeypatch):
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.set_mode(pm.MANUAL)
    _serve_with_accounts(monkeypatch, mode="api-key")

    executed = []
    events = []
    manifest = build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="manual")
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_kw: executed.append((name, args)) or "42",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )

    def wait_hook(tool_name, call_id, _decision, preview):
        pending = ledger.resolve_call(call_id)
        return chat_tool_stream.wait_for_approval(
            ledger, tool_name, pending.digest, call_id, timeout_seconds=5.0, poll_interval=0.05
        )

    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        on_tool_event=events.append,
        wait_for_approval=wait_hook,
    )

    with _http_server(monkeypatch) as port:
        def approver():
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                for evt in list(events):
                    if evt.get("phase") == "awaiting_approval":
                        status, _, _ = _request(
                            port, "POST", f"/v1/approvals/{evt['call_id']}",
                            body="{}", headers={"Content-Type": "application/json",
                                                       "Authorization": "Bearer " + API_KEY},
                        )
                        assert status == 201
                        return
                time.sleep(0.02)

        worker = threading.Thread(target=approver, daemon=True)
        worker.start()
        steps = iter([
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "run_code", "arguments": {"code": "print(42)"}}},
            ]}},
            {"message": {"role": "assistant", "content": "Output was 42."}},
        ])
        outcome = turn.run(lambda _m, _t: next(steps), system="sys", prompt="run", history=None)
        worker.join(timeout=5)

    assert len(executed) == 1
    assert outcome.records[0].status == "ok"
    phases = [e["phase"] for e in events]
    assert "awaiting_approval" in phases and "approved" in phases and "finished" in phases

    # Now verify deny wakes immediately and does NOT execute.
    events.clear()
    executed.clear()
    turn_deny = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        on_tool_event=events.append,
        wait_for_approval=wait_hook,
    )
    with _http_server(monkeypatch) as port:
        def denier():
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                for evt in list(events):
                    if evt.get("phase") == "awaiting_approval":
                        status, _, _ = _request(
                            port, "POST", f"/v1/approvals/deny/{evt['call_id']}",
                            body="{}", headers={"Content-Type": "application/json",
                                                       "Authorization": "Bearer " + API_KEY},
                        )
                        assert status == 200
                        return
                time.sleep(0.02)

        worker = threading.Thread(target=denier, daemon=True)
        worker.start()
        steps = iter([
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "run_code", "arguments": {"code": "print('nope')"}}},
            ]}},
            {"message": {"role": "assistant", "content": "Operator denied the run."}},
        ])
        outcome2 = turn_deny.run(lambda _m, _t: next(steps), system="sys", prompt="run", history=None)
        worker.join(timeout=5)

    assert not executed
    assert outcome2.records[0].status == "denied"
    assert any(e["phase"] == "denied" for e in events)


def test_gate_answer_code_skips_active_tool_turn_and_slash_call_arguments():
    manifest = build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="auto")
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda *_a, **_k: "",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=pm,
    )
    with bind_turn(turn):
        out = server._gate_answer_code(
            "```python\nbad(\n```", iid=None, prompt="p", history=[], model="m",
            system="", temperature=0.2, num_predict=100, num_ctx=2048, cloud=False, tier_label="code",
        )
    assert out == ("```python\nbad(\n```", None, False, {})

    assert developer_chat.slash_call_arguments("/mkdir", "foo/bar") == {"path": "foo/bar", "parents": True}
    assert developer_chat.slash_call_arguments("/run", "print(1)") == {"code": "print(1)", "language": "python"}
    assert developer_chat.slash_call_arguments("/delete", "old.txt")["path"] == "old.txt"
    assert json.loads(json.dumps(developer_chat.slash_call_arguments("/test", "pytest")))


def test_tool_loop_enforces_abort_wall_and_max_tool_calls_inside_batch(monkeypatch):
    from sonder_runtime.domain.chat_tools import ChatToolBudget

    manifest = build_tool_turn_manifest(admitted_tools=["file_read"], user_run_command=False, mode="manual")
    batch = [
        {"function": {"name": "file_read", "arguments": {"path": f"f{i}.txt"}}}
        for i in range(25)
    ]

    # 1) is_aborted() checked inside the batch stops execution at 3 instead of running all 25.
    executed_abort: list[str] = []
    backend_abort = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_k: executed_abort.append(args["path"]) or "ok",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn_abort = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend_abort,
        permission_policy=pm,
        is_aborted=lambda: len(executed_abort) >= 3,
    )
    turn_abort.run(
        lambda _m, _t: {"message": {"role": "assistant", "content": "", "tool_calls": batch}},
        system="sys", prompt="read", history=None,
    )
    assert len(executed_abort) == 3

    # 2) max_tool_calls caps total calls inside a single batch and records budget_exceeded.
    executed_cap: list[str] = []
    backend_cap = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_k: executed_cap.append(args["path"]) or "ok",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn_cap = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend_cap,
        permission_policy=pm,
        budget=ChatToolBudget(max_tool_calls=5, max_model_calls=2),
    )
    steps_cap = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": batch}},
        {"message": {"role": "assistant", "content": "Done after cap."}},
    ])
    outcome_cap = turn_cap.run(lambda _m, _t: next(steps_cap), system="sys", prompt="read", history=None)
    assert len(executed_cap) == 5
    assert any(r.status == "budget_exceeded" for r in outcome_cap.records)
    assert outcome_cap.content == "Done after cap."

    # 3) wall_seconds timeout checked inside the batch stops mid-batch.
    executed_wall: list[str] = []
    clock = [100.0]

    def _tick_dispatch(name, args, **_k):
        executed_wall.append(args["path"])
        clock[0] += 6.0
        return "ok"

    monkeypatch.setattr("sonder_runtime.application.chat.tool_loop.time.monotonic", lambda: clock[0])
    backend_wall = ChatRunBackend(
        dispatch_agent_tool=_tick_dispatch,
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn_wall = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend_wall,
        permission_policy=pm,
        budget=ChatToolBudget(wall_seconds=10.0, max_tool_calls=24),
    )
    turn_wall.run(
        lambda _m, _t: {"message": {"role": "assistant", "content": "", "tool_calls": batch}},
        system="sys", prompt="read", history=None,
    )
    assert len(executed_wall) == 2


def test_tool_loop_rejects_non_string_and_privilege_credential_args():
    manifest = build_tool_turn_manifest(admitted_tools=["file_read"], user_run_command=False, mode="manual")
    dispatched: list[dict] = []
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_k: dispatched.append(args) or "ok",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=pm,
    )
    bad_payloads = [
        {"path": "a.txt", "bypass": True},
        {"path": "a.txt", "bypass": 1},
        {"path": "a.txt", "developer_authorized": True},
        {"path": "a.txt", "extra_roots": ["/etc"]},
        {"path": "a.txt", "token": 12345},
        {"path": "a.txt", "approval": {"once": True}},
    ]
    for bad in bad_payloads:
        obs, _ = turn._run_tool_call(
            {"function": {"name": "file_read", "arguments": bad}}, run_count=0,
        )
        assert "credential arguments" in obs, (bad, obs)
    assert dispatched == []


def test_resolve_agent_project_scope_refuses_network_device_and_control_paths_before_realpath(
    monkeypatch, tmp_path,
):
    import os
    from sonder_runtime.adapters import chat_run_backend

    real_realpath = os.path.realpath
    realpath_calls: list[str] = []

    def _spy_realpath(p, *args, **kwargs):
        realpath_calls.append(str(p))
        return real_realpath(p, *args, **kwargs)

    monkeypatch.setattr(chat_run_backend.os.path, "realpath", _spy_realpath)

    bad_inputs = [
        r"\\nohost.invalid\share",
        r"\\?\UNC\nohost.invalid\share",
        r"\\.\pipe\test",
        r"\\.\NUL",
        r"\\?\GLOBALROOT\Device\Null",
        "//nohost.invalid/share",
        r"\??\C:\Windows",
        "bad\x00root",
        "bad\nroot",
    ]
    for raw in bad_inputs:
        scope, err = chat_run_backend.resolve_agent_project_scope(raw)
        assert scope == "", (raw, scope, err)
        assert err.startswith("ERROR: invalid agent project root:"), (raw, err)
    assert realpath_calls == [], f"os.path.realpath touched forbidden paths: {realpath_calls}"

    # Positive control: an existing local directory still resolves cleanly.
    local_dir = tmp_path / "proj"
    local_dir.mkdir()
    scope, err = chat_run_backend.resolve_agent_project_scope(str(local_dir))
    assert err == ""
    assert scope == real_realpath(str(local_dir))


def test_chat_tool_turn_auto_verifies_and_repairs_broken_final_code_block():
    """If the model emits a broken runnable Python block without testing it, host verifies & repairs."""
    executed: list[str] = []

    def fake_dispatch(name, args, **_kw):
        assert name == "run_code"
        code = str(args.get("code") or "")
        executed.append(code)
        if "undefined_fn" in code:
            return "status: failed\nreturncode: 1\nstderr:\nNameError: name 'undefined_fn' is not defined"
        return "status: ok\nreturncode: 0\nstdout:\n42\n"

    manifest = build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="auto")
    backend = ChatRunBackend(
        dispatch_agent_tool=fake_dispatch,
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        budget=ChatToolBudget(max_runs=3, max_model_calls=4),
    )

    broken_reply = "Here it is:\n```python\ndef solve():\n    return undefined_fn()\nprint(solve())\n```"
    fixed_reply = "Fixed:\n```python\ndef solve():\n    return 42\nprint(solve())\n```"
    seen_messages: list[list[dict]] = []
    replies = iter([
        {"message": {"role": "assistant", "content": broken_reply}},
        {"message": {"role": "assistant", "content": fixed_reply}},
    ])

    def step(msgs, _tools):
        seen_messages.append([dict(m) for m in msgs])
        return next(replies)

    old_mode = pm.current_mode()
    pm.set_mode(pm.AUTO)
    try:
        outcome = turn.run(step, system="sys", prompt="write solver", history=None)
    finally:
        pm.set_mode(old_mode)

    assert outcome.content == fixed_reply
    assert len(executed) == 2
    assert [r.status for r in outcome.records] == ["error", "ok"]
    assert outcome.records[0].exit_code == 1
    assert outcome.records[1].exit_code == 0
    assert any("HOST CODE VERIFICATION FAILED" in str(m.get("content") or "") for m in seen_messages[1])


def test_chat_tool_turn_auto_verify_stops_on_permission_refusal(monkeypatch, tmp_path):
    """Auto-verification respects permission refusals and never loops on denied runs."""
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.set_mode(pm.MANUAL)

    dispatched: list[str] = []
    manifest = build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="manual")
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_kw: dispatched.append(name) or "ok",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        budget=ChatToolBudget(max_runs=3, max_model_calls=4),
    )
    draft = "Here:\n```python\ndef solve():\n    return 42\nprint(solve())\n```"
    calls = 0

    def step(_msgs, _tools):
        nonlocal calls
        calls += 1
        return {"message": {"role": "assistant", "content": draft}}

    outcome = turn.run(step, system="sys", prompt="write solver", history=None)
    assert calls == 1
    assert dispatched == []
    assert outcome.content == draft
    assert [r.status for r in outcome.records] == ["refused"]


def test_wave2_tool_loop_client_exec_skips_auto_verify_blocks_host_and_keeps_tool_call_id():
    from sonder_runtime.application.chat import tool_loop
    from sonder_runtime.domain.chat_tools import ChatToolRecord

    assert "awaiting_client_result" in tool_loop._NON_RETRYABLE_VERIFY_STATUSES

    host_called: list[str] = []
    client_called: list[str] = []
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, _args, **_kw: host_called.append(name) or "host-ran",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )

    def fake_delegate(name, args):
        client_called.append(name)
        return "client-ok", ChatToolRecord("cid-1", name, "ok", "client")

    manifest = build_tool_turn_manifest(admitted_tools=["run_code", "file_read"], user_run_command=False, mode="manual")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=pm,
        client_exec_tools=frozenset({"run_code", "file_read"}),
        client_exec_only=True,
        delegate_client_tool=fake_delegate,
    )

    seen_messages: list[list[dict]] = []
    steps = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_abc1", "function": {"name": "/run_code", "arguments": {"code": "print(1)"}}},
                    {"id": "call_abc2", "function": {"name": "status", "arguments": {}}},
                ],
            }
        },
        # Final reply contains a fenced python block: auto-verify must NOT trigger client_exec run_code!
        {
            "message": {
                "role": "assistant",
                "content": "Here is sample code:\n```python\ndef demo():\n    return 1\n```",
            }
        },
    ])

    def step(msgs, _tools):
        seen_messages.append([dict(m) for m in msgs])
        return next(steps)

    outcome = turn.run(step, system="sys", prompt="test", history=None)
    assert client_called == ["run_code"]
    assert host_called == []
    assert [r.status for r in outcome.records] == ["ok", "refused"]
    # Verify normalized tool_name and preserved tool_call_id on tool messages sent to step 2.
    tool_msgs = [m for m in seen_messages[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["name"] == "run_code"
    assert tool_msgs[0]["tool_call_id"] == "call_abc1"
    assert tool_msgs[1]["name"] == "status"
    assert tool_msgs[1]["tool_call_id"] == "call_abc2"


def test_wave2_chat_tools_wiring_capabilities_project_root_and_slash_wait(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sonder_runtime.bootstrap import chat_tools_wiring

    # 1. capabilities_payload ignores context["account"] unless authorized=True (auth_mode=both disclosure fix).
    partial_ctx = {
        "authorized": False,
        "account": {"username": "alice", "role": "admin"},
    }
    caps = chat_tools_wiring.capabilities_payload(
        partial_ctx,
        auth_mode="both",
        public_hosting=True,
        allow_registration=False,
        require_account=True,
        admin_authorized=False,
    )
    assert "username" not in caps
    assert "role" not in caps
    assert caps["host_bypass_allowed"] is False

    authed_ctx = {
        "authorized": True,
        "account": {"username": "alice", "role": "admin"},
    }
    caps_ok = chat_tools_wiring.capabilities_payload(
        authed_ctx,
        auth_mode="both",
        public_hosting=True,
        allow_registration=False,
        require_account=True,
        admin_authorized=True,
    )
    assert caps_ok["username"] == "alice"
    assert caps_ok["role"] == "admin"
    assert caps_ok["host_bypass_allowed"] is True

    # 2. build_http_chat_tool_turn resolves project_root via server.served_work_project (drops UNC & bare namespaces).
    monkeypatch.setattr(chat_tools_wiring, "_model_supports_tools", lambda *_a, **_k: True)
    pm.set_mode(pm.AUTO)
    handler_stub = SimpleNamespace(_early_stream=SimpleNamespace(completion_id="iid-1", client_gone=False, lock=threading.Lock()))

    turn_unc = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=pm,
        handler=handler_stub,
        chunk_fn=lambda *_a, **_k: "",
        model="qwen",
        project=r"\\nohost.invalid\share",
        stream=False,
        authorized_tools=True,
    )
    assert turn_unc is not None and turn_unc.backend._project_root == ""

    turn_ns = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=pm,
        handler=handler_stub,
        chunk_fn=lambda *_a, **_k: "",
        model="qwen",
        project="default",
        stream=False,
        authorized_tools=True,
    )
    assert turn_ns is not None and turn_ns.backend._project_root == ""

    # 3. maybe_open_mcp_chat_tool_turn resolves project_root via resolve_agent_project_scope.
    mcp_turn_unc = chat_tools_wiring.maybe_open_mcp_chat_tool_turn(
        server=server,
        permission_policy=pm,
        model="qwen",
        cloud=False,
        project=r"\\nohost.invalid\share",
    )
    assert mcp_turn_unc is not None and mcp_turn_unc.backend._project_root == ""

    mcp_turn_dir = chat_tools_wiring.maybe_open_mcp_chat_tool_turn(
        server=server,
        permission_policy=pm,
        model="qwen",
        cloud=False,
        project=str(tmp_path),
    )
    assert mcp_turn_dir is not None and mcp_turn_dir.backend._project_root != ""

    # 4. run_client_slash_turn forwards is_aborted=early.client_gone and on_wait=write_sse_comment_fn.
    wait_kwargs_seen: dict = {}

    class FakeCall:
        call_id = "call-slash-1"

        def sse_event(self, phase):
            return {"phase": phase, "call_id": self.call_id}

    class FakeBroker:
        def create_call(self, **_kw):
            return FakeCall()

        def wait_for_result(self, call_id, *, timeout_seconds=None, heartbeat_interval=5.0, on_wait=None, is_aborted=None):
            wait_kwargs_seen["call_id"] = call_id
            wait_kwargs_seen["on_wait"] = on_wait
            wait_kwargs_seen["is_aborted"] = is_aborted
            return SimpleNamespace(ok=True, denied=False, status="ok", output="ran!", error="", elapsed_ms=5)

    early_obj = SimpleNamespace(completion_id="iid-s", client_gone=False, lock=threading.Lock())
    slash_handler = SimpleNamespace(
        _early_stream=early_obj,
        _begin_early_stream=lambda _m: None,
        _pause_early_stream=lambda: None,
    )
    sse_ping = lambda: None
    plan = SimpleNamespace(tool="run_code", args={"code": "print(1)"}, risk="execute")
    records: list[dict] = []
    handoffs: list[dict] = []
    reply, iid = chat_tools_wiring.run_client_slash_turn(
        slash_handler,
        plan,
        broker=FakeBroker(),
        principal="user:bob",
        session="s1",
        model="qwen",
        stream=True,
        chunk_fn=lambda *_a, **_k: "",
        write_tool_chunk_fn=lambda *_a, **_k: True,
        slash_records=records,
        slash_handoffs=handoffs,
        write_sse_comment_fn=sse_ping,
    )
    assert reply == "ran!" and iid == "iid-s"
    assert wait_kwargs_seen["on_wait"] is sse_ping
    assert callable(wait_kwargs_seen["is_aborted"])
    assert wait_kwargs_seen["is_aborted"]() is False
    early_obj.client_gone = True
    assert wait_kwargs_seen["is_aborted"]() is True
