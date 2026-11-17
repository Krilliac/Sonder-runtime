"""Tests for depth-1 agent delegation, peer board coordination, SSE events, #74, and #78 fixes."""
from __future__ import annotations

import contextlib
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import command_catalog
import master_orchestrator
import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as serve
from sonder_runtime.adapters.chat_run_backend import ChatRunBackend
from sonder_runtime.adapters.inference import gpu_admission
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.agents import delegation_runtime as rt
from sonder_runtime.application.chat.tool_loop import ChatToolTurn, build_tool_turn_manifest
from sonder_runtime.domain.chat_tools import chat_tools_payload


@pytest.fixture(autouse=True)
def _restore_modes():
    prev_state = dict(pm._STATE)
    yield
    pm._STATE.clear()
    pm._STATE.update(prev_state)
    rt.clear_ephemeral_state()


def test_depth_1_delegation_allowed_and_depth_2_blocked(monkeypatch, tmp_path):
    pm.set_mode(pm.AUTO)
    seen = []

    def fake_agent_impl(prompt, **kwargs):
        seen.append((rt.current_depth(), rt.current_agent_id(), rt.current_agent_role(), kwargs.get("read_only")))
        # Depth-1 child sees delegation stripped from both full and compact help
        assert "delegate_task" not in server._agent_tool_help()
        assert "spawn_agents" not in server._agent_tool_help()
        assert "delegate_task" not in server._agent_tool_help(num_ctx=2048)
        assert "spawn_agents" not in server._agent_tool_help(num_ctx=2048)
        # Direct dispatch at depth 1 fails closed
        refused = server._agent_dispatch("delegate_task", {"task": "nested"})
        assert "blocked at delegation depth 1" in refused
        refused_batch = server._agent_dispatch("spawn_agents", {"tasks": [{"task": "nested"}]})
        assert "blocked at delegation depth 1" in refused_batch
        return f"done by {rt.current_agent_id()}"

    monkeypatch.setattr(server, "_agent_impl", fake_agent_impl)
    out = server._agent_dispatch(
        "delegate_task",
        {"task": "Inspect file.py", "name": "scout"},
        repository_extra_roots=str(tmp_path),
    )
    assert "delegation completed: 1/1 succeeded" in out
    assert "@scout" in out
    assert seen == [(1, "scout", "scout", False)]


def test_parallel_spawn_agents_main_and_node1_and_master_status(monkeypatch):
    pm.set_mode(pm.AUTO)
    in_flight = []
    live_snapshots = []

    class FakePool:
        enabled = True

        def pool_capacity_summary(self):
            return {"total_slots": 3, "healthy_worker_count": 2, "workers": []}

        @contextlib.contextmanager
        def local_agent_admission(self, agent_id):
            in_flight.append(agent_id)
            yield

        def last_route(self):
            return "https://10.77.0.2:8443"

    monkeypatch.setattr(server, "OLLAMA_POOL", FakePool())

    def fake_agent_impl(prompt, **kwargs):
        time.sleep(0.02)
        snap = master_orchestrator.snapshot(include_finished=True, limit=20)
        live_snapshots.append([row["id"] for row in snap["agents"]])
        return f"finished:{rt.current_agent_id()}"

    monkeypatch.setattr(server, "_agent_impl", fake_agent_impl)
    out = server._agent_dispatch(
        "spawn_agents",
        {
            "tasks": [
                {"task": "Map symbols", "name": "scout"},
                {"task": "Implement helper", "name": "coder"},
                {"task": "Check boundaries", "name": "security"},
            ],
        },
    )
    assert "delegation completed: 3/3 succeeded" in out
    assert set(in_flight) == {"scout", "coder", "security"}
    assert any(ids for ids in live_snapshots)
    final_snap = master_orchestrator.snapshot(include_finished=True, limit=20)
    assert any(row["id"] in {"scout", "coder", "security"} for row in final_snap["agents"])


def test_parent_holding_gpu_ticket_cannot_deadlock_delegation():
    pm.set_mode(pm.AUTO)
    token = gpu_admission._HELD_TICKET.set(object())
    try:
        out = server._agent_dispatch("delegate_task", {"task": "blocked while GPU slot held"})
        assert "active GPU admission ticket" in out
    finally:
        gpu_admission._HELD_TICKET.reset(token)


def test_security_role_read_only_and_project_boundary_preserved(monkeypatch, tmp_path):
    pm.set_mode(pm.AUTO)
    inside = tmp_path / "repo"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()

    # 1. Parent read_only=True refuses delegation directly, and explicit read_only=True child spec propagates
    ro_refused = server._agent_dispatch(
        "delegate_task",
        {"task": "check repo"},
        read_only=True,
        repository_extra_roots=str(inside),
    )
    assert "read-only" in ro_refused.lower()

    seen_ro = []

    def fake_impl(prompt, **kwargs):
        seen_ro.append(kwargs.get("read_only"))
        return "ok"

    monkeypatch.setattr(server, "_agent_impl", fake_impl)
    server._agent_dispatch(
        "delegate_task",
        {"task": "check repo", "read_only": True},
        repository_extra_roots=str(inside),
    )
    assert seen_ro == [True]

    refused_escape = server._agent_dispatch(
        "delegate_task",
        {"task": "escape", "cwd": str(outside)},
        repository_extra_roots=str(inside),
    )
    assert "exceeds parent project boundary" in refused_escape

    # 2. User role cannot delegate or escalate inside child
    with role_tool_policy.caller_role_scope("user"):
        role_err = server._agent_permission_gate_error("delegate_task", {"task": "nope"})
        assert "user" in role_err.lower() or "role" in role_err.lower()

    # 3. Mode.BYPASS carries into child thread while keeping operator tools blocked
    with pm.attended_mode_change(), pm.attended_bypass_confirm():
        pm.set_mode(pm.BYPASS)
    child_modes = []

    def check_bypass(prompt, **kwargs):
        child_modes.append(pm.current_mode())
        op_blocked = server._agent_dispatch("permission_approve", {"call_id": "x"})
        assert "system operation" in op_blocked
        return "bypass-ok"

    monkeypatch.setattr(server, "_agent_impl", check_bypass)
    out = server._agent_dispatch("delegate_task", {"task": "run in bypass"})
    assert "delegation completed: 1/1 succeeded" in out
    assert child_modes == [pm.BYPASS]


def test_peer_board_coordination_step_injection_and_sse_events(monkeypatch):
    pm.set_mode(pm.AUTO)
    agent_events = []
    board_events = []

    board = rt.AgentProgressBoard(run_id="run-peer-1")
    unsub_a = board.subscribe(lambda ev: agent_events.append(ev.to_dict()))
    unsub_b = board.subscribe_board(lambda msg: board_events.append(msg.to_dict()))
    try:
        with rt.bind_board(board):
            with rt.agent_identity_scope("scout", role="scout"):
                posted = json.loads(server._agent_dispatch(
                    "post_to_board",
                    {"text": "@coder found config in serve.py:7030, nice catch!", "kind": "handoff"},
                ))
                assert posted["ok"] is True
                assert posted["agent"] == "scout"
                assert posted["role"] == "scout"
                assert posted["mentions"] == ["coder"]

            with rt.agent_identity_scope("coder", role="coder"):
                read_res = json.loads(server._agent_dispatch(
                    "read_board",
                    {"since_seq": 0, "mentions_only": True},
                ))
                assert len(read_res["messages"]) == 1
                assert read_res["messages"][0]["kind"] == "handoff"
                digest, cursor = rt.unseen_peer_board_digest(0)
                assert "[MENTIONED YOU]" in digest
                assert "@scout" in digest
                assert cursor == 1
                prog = json.loads(server._agent_dispatch(
                    "report_progress",
                    {"summary": "patching config", "thinking": "checked 7030"},
                ))
                assert prog["recorded"] is True
    finally:
        unsub_a()
        unsub_b()

    assert any(ev["phase"] == "progress" and ev["agent_id"] == "coder" for ev in agent_events)
    assert len(board_events) == 1 and board_events[0]["agent"] == "scout"

    # Verify _chunk emits backwards-compatible sonder_agent & sonder_agent_board frames
    frame_a = serve._chunk("id1", "qwen", {}, sonder_agent=agent_events[0])
    frame_b = serve._chunk("id1", "qwen", {}, sonder_agent_board=board_events[0])
    parsed_a = json.loads(frame_a.split("data: ", 1)[1])
    parsed_b = json.loads(frame_b.split("data: ", 1)[1])
    assert parsed_a["sonder_agent"]["run_id"] == "run-peer-1"
    assert parsed_b["sonder_agent_board"]["kind"] == "handoff"


def test_chat_tool_turn_depth_and_sse_streaming_and_peer_injection(monkeypatch):
    # Depth-1 excludes delegation tools from ChatToolTurn tools_payload & refuses call
    with rt.depth_scope(1):
        tools = chat_tools_payload(depth=rt.current_depth())
        names = {item["function"]["name"] for item in tools}
        assert "delegate_task" not in names and "spawn_agents" not in names
        assert "report_progress" in names and "post_to_board" in names

    # Streaming ChatToolTurn emits sonder_agent & sonder_agent_board events and injects board updates
    pm.set_mode(pm.AUTO)
    seen_agent = []
    seen_board = []
    seen_prompts = []
    backend = ChatRunBackend(
        dispatch_agent_tool=server._agent_dispatch,
        catalog_provider=lambda: {},
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    turn = ChatToolTurn(
        manifest=build_tool_turn_manifest(admitted_tools=["post_to_board", "read_board"], user_run_command=True, mode="auto"),
        tools_payload=chat_tools_payload(allowed_names=["post_to_board", "read_board"]),
        backend=backend,
        permission_policy=pm,
        on_agent_event=seen_agent.append,
        on_board_event=seen_board.append,
    )
    calls = [0]

    def fake_step(messages, tools):
        calls[0] += 1
        seen_prompts.append(list(messages))
        if calls[0] == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "<think>share note with team</think>",
                    "tool_calls": [{"function": {"name": "post_to_board", "arguments": {"text": "ready", "kind": "status"}}}],
                },
            }
        if calls[0] == 2:
            # Peer posts asynchronously between steps
            rt.post_to_board("@coordinator peer check ok!", kind="praise")
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "read_board", "arguments": {}}}],
                },
            }
        return {"message": {"role": "assistant", "content": "Done!"}}

    outcome = turn.run(fake_step, system="sys", prompt="go")
    assert outcome.content == "Done!"
    assert any(ev["phase"] == "thinking" and "share note" in ev["thinking"] for ev in seen_agent)
    assert any(ev["phase"] == "completed" for ev in seen_agent)
    assert len(seen_board) >= 1


def test_p0_chat_tool_turn_double_approval_unmocked_end_to_end(monkeypatch, tmp_path):
    """#74.1: One-shot approval granted mid-turn must NOT fail on double decide() in _agent_dispatch."""
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)
    pm.set_mode(pm.MANUAL)

    backend = ChatRunBackend(
        dispatch_agent_tool=server._agent_dispatch,
        catalog_provider=lambda: {},
        permission_policy=pm,
        blocked_tools=frozenset(),
    )
    tool_events = []

    def wait_and_approve_once(tool_name, call_id, decision, preview):
        pending = ledger.resolve_call(call_id)
        ledger.issue(tool_name, pending.digest, approver="operator")
        return "approved"

    turn = ChatToolTurn(
        manifest=build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="manual"),
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        on_tool_event=tool_events.append,
        wait_for_approval=wait_and_approve_once,
    )
    step_no = [0]

    def step_fn(messages, tools):
        step_no[0] += 1
        if step_no[0] == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "run_code", "arguments": {"code": "print(40+2)", "language": "python"}}}],
                },
            }
        return {"message": {"role": "assistant", "content": "42"}}

    outcome = turn.run(step_fn, system="sys", prompt="compute 40+2")
    assert outcome.content == "42"
    phases = [ev["phase"] for ev in tool_events]
    assert "awaiting_approval" in phases
    assert "approved" in phases
    finished = next(ev for ev in tool_events if ev["phase"] == "finished")
    assert finished["ok"] is True


def test_74_compact_tool_help_no_denied_leak_and_run_code_lookup_and_mkdir(tmp_path):
    # #74.2: Compact help in plan and manual modes never leaks file_delete / admin_login / elevate
    for mode in (pm.PLAN, pm.MANUAL):
        pm.set_mode(mode)
        compact = server._agent_tool_help(num_ctx=2048)
        for forbidden in ("admin_login", "elevate", "permission_approve"):
            assert forbidden not in compact
        if mode == pm.PLAN:
            assert "file_delete" not in compact

    # #74.3: tool_help("run_code") resolves even when cwd/project dir passed without strict_project_tools
    pm.set_mode(pm.AUTO)
    found = server._agent_dispatch("tool_help", {"name": "run_code"}, repository_extra_roots=str(tmp_path))
    assert "run_code" in found and "unknown tool" not in found

    # #74.4: /mkdir narrows cleanly to single ('directory_create',) so _http_tool_refusal preserves call_id
    assert command_catalog.http_slash_tools().get("/mkdir") == ("directory_create",)
    ledger = ApprovalLedger(tmp_path / "mkdir_approvals.db")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pm, "_approval_ledger", lambda: ledger)
        pm.set_mode(pm.MANUAL)
        mp.setenv("SONDER_HTTP_ALLOW_LOCAL_OPEN", "1")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
                data=json.dumps({
                    "model": "code",
                    "messages": [{"role": "user", "content": "/mkdir sub_dir"}],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            refusal = (payload.get("sonder_receipt") or {}).get("refusal") or {}
            assert refusal.get("call_id"), payload
        finally:
            srv.shutdown()
            srv.server_close()


def test_78_node1_direct_model_pin_and_cluster_capacity(monkeypatch):
    class FakeSonderGateway:
        def unified_worker_status(self, ollama_pool=None):
            return {
                "available_models": ["qwen3.6:35b", "astra-cyber-35b-a3b:latest"],
                "workers": [
                    {"source": "sonder_inference_worker", "origin": "https://10.77.0.2:8443", "healthy": True, "max_inflight": 2},
                ],
            }

    monkeypatch.setattr(server, "_get", lambda *a, **k: {"models": []})
    monkeypatch.setattr(
        server, "_APP_GRAPH",
        SimpleNamespace(model_gateway=SimpleNamespace(providers={"sonder_inference": FakeSonderGateway()})),
        raising=False,
    )
    pin = server._resolve_model_pin("qwen3.6:35b")
    assert pin.ok and pin.model == "qwen3.6:35b"

    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "1")
    cap = master_orchestrator.capacity()
    assert cap["ollama_num_parallel"] >= 3
    assert cap["automatic_worker_slots"] >= 3
