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
