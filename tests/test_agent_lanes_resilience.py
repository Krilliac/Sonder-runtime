"""Resilience, lifecycle, concurrency, and bounded-serialization tests for agent lanes.

Covers ``sonder_runtime/interfaces/agent_lanes.py``,
``sonder_runtime/application/agents/interactive_lanes.py``, and
``sonder_runtime/interfaces/http/background_work.py``.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import threading
import time
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import (
    AgentLaneService,
    _LaneCancellation,
    _WAIT_LOCK,
    _WAIT_OWNERS,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.loop import LoopSessionLifecycleFacade
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.interfaces.agent_lanes import dispatch_agent_lane_tool
from sonder_runtime.interfaces.http.background_work import (
    BackgroundWorkAggregator,
    MAX_FLEET_CHILDREN,
    MAX_ID_CHARS,
    MAX_PHASE_CHARS,
    MAX_PROJECT_CHARS,
    MAX_STATUS_CHARS,
    dispatch_background_work_route,
)


class _ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, request, context):
        self.calls += 1
        item = self._responses.pop(0)
        if callable(item):
            item = item(request, context)
        if isinstance(item, Exception):
            raise item
        return ModelResponse(item, "fake", request.tier, tokens_out=10)


class _FakeTools:
    def __init__(self, result_fn):
        self._result_fn = result_fn
        self.calls = []
        descriptor = SimpleNamespace(
            name="read_file",
            effects=(SimpleNamespace(name="read_files"),),
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        self.graph = SimpleNamespace(
            registry=SimpleNamespace(
                list_all=lambda: (descriptor,),
                get=lambda name: descriptor,
            )
        )

    def execute(self, request):
        self.calls.append(request)
        return self._result_fn(request)


@pytest.fixture
def lane_env(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    workspace = tmp_path / "work"
    workspace.mkdir()
    context = local_owner_context(correlation_id="resilience", workspace_roots=(tmp_path,))
    services = []

    def make_service(model=None, tools=None, **kwargs):
        service = AgentLaneService(
            store,
            sessions,
            model or _ScriptedModel(["done"]),
            tools=tools,
            auto_start=kwargs.pop("auto_start", False),
            **kwargs,
        )
        services.append(service)
        return service

    yield make_service, store, sessions, context, workspace
    for service in services:
        service.close()


# ---------------------------------------------------------------------------
# sonder_runtime/interfaces/agent_lanes.py
# ---------------------------------------------------------------------------


def test_dispatch_rejects_empty_and_non_string_parent_binding(lane_env):
    make_service, _, _, context, _ = lane_env
    service = make_service()
    for parent_id, bound_id in (
        ("", ""),
        ("   ", "   "),
        ("parent", None),
        ("parent", ""),
        ("parent", "other"),
        (123, 123),
    ):
        with pytest.raises(PermissionError, match="bound"):
            dispatch_agent_lane_tool(
                service,
                "list",
                {},
                context,
                parent_id,
                bound_parent_session_id=bound_id,
            )


def test_dispatch_rejects_unhashable_unknown_actions_and_bad_payloads(lane_env):
    make_service, _, _, context, _ = lane_env
    service = make_service()
    for bad_action in (["cancel"], {"action": "list"}, None, 42, "delete"):
        with pytest.raises(ValueError, match="unknown agent lane action"):
            dispatch_agent_lane_tool(
                service,
                bad_action,
                {},
                context,
                "parent",
                bound_parent_session_id="parent",
            )
    with pytest.raises(ValueError, match="bounded object"):
        dispatch_agent_lane_tool(
            service, "list", "not-a-dict", context, "parent",
            bound_parent_session_id="parent",
        )
    with pytest.raises(ValueError, match="bounded object"):
        dispatch_agent_lane_tool(
            service,
            "list",
            {f"k{i}": i for i in range(33)},
            context,
            "parent",
            bound_parent_session_id="parent",
        )
    with pytest.raises(ValueError, match="keys must be strings"):
        dispatch_agent_lane_tool(
            service, "list", {1: "bad"}, context, "parent",
            bound_parent_session_id="parent",
        )
    with pytest.raises(PermissionError, match="trusted invocation"):
        dispatch_agent_lane_tool(
            service,
            "list",
            {"bound_parent_session_id": "parent"},
            context,
            "parent",
            bound_parent_session_id="parent",
        )
    with pytest.raises(ValueError, match="unexpected argument"):
        dispatch_agent_lane_tool(
            service,
            "cancel",
            {"lane_id": "lane-" + "a" * 32, "command_id": "c1", "action": "resume"},
            context,
            "parent",
            bound_parent_session_id="parent",
        )
    with pytest.raises(ValueError, match="unexpected argument"):
        dispatch_agent_lane_tool(
            service,
            "retrieve_archive",
            {"lane_id": "lane-" + "a" * 32, "archive_id": "a1", "extra": True},
            context,
            "parent",
            bound_parent_session_id="parent",
        )


def test_dispatch_ack_and_lane_id_validation_and_spawn_with_canonical_lane_id(lane_env):
    make_service, _, _, context, workspace = lane_env
    service = make_service()
    with pytest.raises(ValueError, match="report_id required"):
        dispatch_agent_lane_tool(
            service,
            "ack",
            {"command_id": "ack-1"},
            context,
            "parent",
            bound_parent_session_id="parent",
        )
    for bad_lane in ("   ", 123, ["lane-1"], "lane-\x00bad"):
        with pytest.raises(ValueError, match="lane_id"):
            dispatch_agent_lane_tool(
                service,
                "inspect",
                {"lane_id": bad_lane},
                context,
                "parent",
                bound_parent_session_id="parent",
            )
    explicit_id = "lane-" + "a" * 32
    receipt = dispatch_agent_lane_tool(
        service,
        "spawn",
        {
            "command_id": "spawn-explicit",
            "task": "bounded work",
            "workspace_root": str(workspace),
            "lane_id": explicit_id,
        },
        context,
        "parent",
        bound_parent_session_id="parent",
    )
    assert receipt["lane"]["id"] == explicit_id


# ---------------------------------------------------------------------------
# sonder_runtime/application/agents/interactive_lanes.py
# ---------------------------------------------------------------------------


def test_close_releases_owner_lock_file_and_is_idempotent(lane_env):
    make_service, store, _, _, _ = lane_env
    service = make_service(auto_start=True)
    assert store.owner_definitely_stopped(service.owner) is False

    service.close()
    assert store.owner_definitely_stopped(service.owner) is True
    # Calling close() repeatedly is a safe no-op.
    service.close()
    assert store.owner_definitely_stopped(service.owner) is True


def test_init_validation_failure_does_not_leak_owner_lock(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    acquired = []
    real_acquire = store.acquire_owner

    def spy_acquire(owner):
        lease = real_acquire(owner)
        acquired.append((owner, lease))
        return lease

    store.acquire_owner = spy_acquire
    with pytest.raises(TypeError, match="loop_factory"):
        AgentLaneService(
            store, sessions, _ScriptedModel(["ok"]), loop_factory="not-callable"
        )
    assert acquired == []


def test_close_defers_owner_lease_release_until_inflight_worker_finishes(lane_env):
    make_service, store, _, context, workspace = lane_env
    entered = threading.Event()
    release = threading.Event()

    def slow_reply(_request, _ctx):
        entered.set()
        release.wait(5)
        return "finished cleanly"

    service = make_service(model=_ScriptedModel([slow_reply]), auto_start=True)
    lane_id = service.spawn(
        command_id="spawn-inflight",
        parent_session_id="parent",
        task="run slow",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    assert entered.wait(5)

    # Closing while a worker is mid-call keeps the lock held until the worker exits.
    service.close()
    assert store.owner_definitely_stopped(service.owner) is False

    release.set()
    deadline = time.monotonic() + 5.0
    while not store.owner_definitely_stopped(service.owner) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.owner_definitely_stopped(service.owner) is True
    assert store.read_lane(lane_id)["status"] in {"completed", "failed", "interrupted"}


def test_wait_validates_inputs_before_slot_and_honors_deadline_and_close(lane_env):
    make_service, _, _, context, workspace = lane_env
    service = make_service()
    lane_id = service.spawn(
        command_id="spawn-wait",
        parent_session_id="parent",
        task="wait test",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    cursor = service.inspect(lane_id, context)["next_cursor"]

    for bad_timeout in (True, False, float("nan"), float("inf"), -1, 31):
        with pytest.raises(ValueError, match="timeout"):
            service.wait(lane_id, context, cursor=cursor, timeout_seconds=bad_timeout)
    with _WAIT_LOCK:
        assert _WAIT_OWNERS.get(context.principal_id, 0) == 0

    # Short caller deadline stops wait() promptly instead of blocking 25s.
    short_ctx = replace(context, deadline_monotonic=time.monotonic() + 0.08)
    t0 = time.monotonic()
    result = service.wait(lane_id, short_ctx, cursor=cursor, timeout_seconds=25)
    elapsed = time.monotonic() - t0
    assert result["events"] == []
    assert elapsed < 1.5

    # Closing the service wakes a blocked waiter immediately.
    started_wait = threading.Event()
    wait_done = threading.Event()

    def run_wait():
        started_wait.set()
        service.wait(lane_id, context, cursor=cursor, timeout_seconds=25)
        wait_done.set()

    worker = threading.Thread(target=run_wait)
    worker.start()
    assert started_wait.wait(2)
    time.sleep(0.05)
    service.close()
    assert wait_done.wait(2)
    worker.join(2)
    with _WAIT_LOCK:
        assert _WAIT_OWNERS.get(context.principal_id, 0) == 0


def test_lane_cancellation_fail_closed_on_missing_lane_and_bad_timeout(lane_env):
    make_service, _, _, _, _ = lane_env
    service = make_service()
    cancel = _LaneCancellation(service, "lane-does-not-exist", "attempt-1")
    assert cancel.cancelled is True
    for bad in (True, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            cancel.wait(bad)


def test_run_pending_releases_ownership_if_post_claim_setup_fails(lane_env, monkeypatch):
    make_service, store, _, context, workspace = lane_env
    service = make_service()
    lane_id = service.spawn(
        command_id="spawn-post-claim-fail",
        parent_session_id="parent",
        task="test post-claim failure",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]

    monkeypatch.setattr(
        service, "_resume_barrier", lambda _lane: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    service.run_pending(lane_id, context)

    lane = store.read_lane(lane_id)
    assert lane["status"] == "failed"
    assert lane["owner"] == ""
    assert lane["error"] == "LANE_ATTEMPT_FAILED"


def test_run_pending_managed_authority_handles_none_deadline_and_bind_failure(lane_env):
    make_service, store, _, base_context, workspace = lane_env

    class _FakeManagedAuthority:
        def __init__(self, *, fail_bind: bool):
            self.fail_bind = fail_bind
            self.bound = 0
            self.released = 0

        def admit(self, _parent_session_id, _context):
            from contextlib import nullcontext

            return nullcontext(object())

        def authorize_lane(self, _admission, _lane, _context, *, connection=None):
            return None

        def bind_worker(self, _lane, _context, _run_context, *, issuer):
            self.bound += 1
            if self.fail_bind:
                raise PermissionError("bind rejected")

        def release_worker(self, _run_context, *, issuer):
            self.released += 1

    managed_ctx = replace(
        base_context,
        principal_id="acct-managed",
        auth_level="authenticated",
        source="http",
        correlation_id="managed-test",
        deadline_monotonic=None,
    )
    service = make_service(model=_ScriptedModel(["ok"]))
    auth_fail = _FakeManagedAuthority(fail_bind=True)
    service.managed_authority = auth_fail

    def insert_managed_lane(suffix: str) -> str:
        with store.transaction() as tx:
            root_id = tx.root("managed-session", managed_ctx.principal_id)
            lane = {
                "id": "lane-" + suffix * 32,
                "session_id": "lane-session-" + suffix * 32,
                "parent_lane_id": None,
                "parent_session_id": "managed-session",
                "title": "managed",
                "task": "managed task",
                "status": "queued",
                "revision": 1,
                "attempt_id": "attempt-managed-" + suffix,
                "workspace_root": str(workspace),
                "tier": "code",
                "expensive_tier": False,
                "principal_id": managed_ctx.principal_id,
                "auth_level": managed_ctx.auth_level,
                "mailbox_parent": root_id,
                "root_id": root_id,
                "owner": "",
                "cloud_allowed": False,
                "remote_ollama_allowed": False,
                "grant_expires": time.time() + 300,
                "grant_id": "grant-" + suffix,
                "grant_revision": 1,
                "grant_policy": "local-owner-file-scope-v1",
                "allowed_tools": [],
                "max_steps": 4,
                "max_output_tokens": 1024,
                "max_wall_seconds": 60,
                "used_steps": 0,
                "used_tokens": 0,
                "used_wall": 0.0,
                "pending_effect": False,
                "pending_response": None,
                "depth": 1,
                "error": "",
                "artifacts": [],
                "archived_at": None,
                "archive_tombstone": None,
            }
            tx.insert(lane)
            tx.message(lane, "managed task", "parent")
            return lane["id"]

    fail_lane_id = insert_managed_lane("b")
    service._schedule(fail_lane_id, managed_ctx)
    service.run_pending(fail_lane_id, managed_ctx)

    # bind_worker failed -> release_worker must NOT be called, dispatch proof cleared, owner released.
    assert auth_fail.bound == 1
    assert auth_fail.released == 0
    assert fail_lane_id not in service._app_dispatch
    saved = store.read_lane(fail_lane_id)
    assert saved["owner"] == ""
    assert saved["status"] == "failed"
    assert saved["error"] == "AUTHORITY_DENIED"

    # With fail_bind=False and deadline_monotonic=None, managed worker succeeds and releases cleanly.
    auth_ok = _FakeManagedAuthority(fail_bind=False)
    service.managed_authority = auth_ok
    ok_lane_id = insert_managed_lane("c")
    service._schedule(ok_lane_id, managed_ctx)
    service.run_pending(ok_lane_id, managed_ctx)

    assert auth_ok.bound == 1
    assert auth_ok.released == 1
    assert ok_lane_id not in service._app_dispatch
    completed = store.read_lane(ok_lane_id)
    assert completed["status"] == "completed"
    assert completed["owner"] == ""


def test_mid_turn_verification_barrier_yields_without_busy_spin_and_resumes(lane_env):
    make_service, store, _, context, workspace = lane_env
    target = workspace / "note.txt"
    target.write_text("hello", encoding="utf-8")

    tool_call = json.dumps({"tool": "read_file", "arguments": {"path": str(target)}})
    model = _ScriptedModel([tool_call, "final verified report"])
    tools = _FakeTools(lambda _req: SimpleNamespace(success=True, output="hello", error_code=""))
    service = make_service(model=model, tools=tools)

    lane_id = service.spawn(
        command_id="spawn-barrier",
        parent_session_id="parent",
        task="read note",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]

    # Activate verification barrier right after the model returns the tool call.
    real_generate = model.generate

    def generate_and_block(request, ctx):
        response = real_generate(request, ctx)
        if model.calls == 1:
            with store.transaction() as tx:
                tx.acquire_verification_barrier("parent", context.principal_id, "verify-1")
        return response

    model.generate = generate_and_block

    service.run_pending(lane_id, context)

    # Must yield cleanly without busy-spinning or executing the tool yet.
    assert len(tools.calls) == 0
    assert model.calls == 1
    paused = store.read_lane(lane_id)
    assert paused["status"] == "queued"
    assert paused["owner"] == ""
    assert paused["pending_response"] is not None
    assert lane_id in service._deferred_verification

    # Release barrier, resume deferred verification, and complete without re-calling model for step 1.
    with store.transaction() as tx:
        tx.release_verification_barrier("parent", context.principal_id, "verify-1")
    service.resume_after_verification("parent")
    assert lane_id not in service._deferred_verification
    service.run_pending(lane_id, context)

    assert len(tools.calls) == 1
    assert model.calls == 2
    finished = store.read_lane(lane_id)
    assert finished["status"] == "completed"
    assert finished["owner"] == ""


def test_resume_after_verification_skips_missing_or_cancelled_lanes(lane_env):
    make_service, _, _, context, workspace = lane_env
    service = make_service()
    lane_id = service.spawn(
        command_id="spawn-defer-skip",
        parent_session_id="parent",
        task="defer test",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    service._remember_deferred_verification("lane-missing-id", context)
    service._remember_deferred_verification(lane_id, context)

    service.control(lane_id, "cancel", command_id="cancel-deferred", context=context)
    # Cancelling evicted lane_id immediately; missing lane is cleaned on resume without raising.
    assert lane_id not in service._deferred_verification
    service.resume_after_verification("parent")
    assert "lane-missing-id" not in service._deferred_verification


def test_loop_steering_sequence_cleaned_up_on_finish(lane_env):
    make_service, _, sessions, context, workspace = lane_env
    loop = LoopSessionLifecycleFacade(sessions)
    service = make_service(model=_ScriptedModel(["done"]), loop=loop)
    lane_id = service.spawn(
        command_id="spawn-steer-cleanup",
        parent_session_id="parent",
        task="steer cleanup",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    lane = service.store.read_lane(lane_id)
    service._loop_binding(lane, create=True)
    service._loop_steer(
        lane,
        command_id="steer-1",
        content="adjust",
        author="user",
        message_id="msg-1",
    )
    assert lane["attempt_id"] in service._loop_steering_sequence

    service._loop_finish(lane, "completed")
    assert lane["attempt_id"] not in service._loop_steering_sequence


def test_execute_tool_sanitizes_non_json_nested_cyclic_and_non_finite_outputs(lane_env):
    make_service, store, _, context, workspace = lane_env
    target = workspace / "sample.txt"
    target.write_text("data", encoding="utf-8")

    cyclic: dict = {
        "path": target,
        "raw": b"\xffhello",
        "nan_val": float("nan"),
        "inf_val": float("inf"),
        "tags": {"b", "a"},
    }
    cyclic["self"] = cyclic

    tool_call = json.dumps({"tool": "read_file", "arguments": {"path": str(target)}})
    model = _ScriptedModel([tool_call, "all clear"])
    tools = _FakeTools(lambda _req: SimpleNamespace(success=True, output=cyclic, error_code=""))
    service = make_service(model=model, tools=tools)

    lane_id = service.spawn(
        command_id="spawn-sanitize",
        parent_session_id="parent",
        task="read weird output",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane_id, context)

    lane = store.read_lane(lane_id)
    assert lane["status"] == "completed"
    assert lane["pending_effect"] is False
    events, _ = store.events(lane_id, 0, 50)
    tool_result = next(ev for ev in events if ev["event_type"] == "tool.result")
    output = tool_result["payload"]["output"]
    assert output["path"] == str(target)
    assert output["self"] == "<cycle>"
    assert output["nan_val"] == "nan"
    assert output["inf_val"] == "inf"
    assert output["tags"] == ["a", "b"]
    # Serializes strictly with allow_nan=False.
    json.dumps(tool_result, allow_nan=False)


def test_run_pending_exception_does_not_finalize_other_owners_loop_turn(lane_env, monkeypatch):
    make_service, store, _, context, workspace = lane_env
    finish_calls = []

    def hijack_and_fail(request, ctx):
        with store.transaction() as tx:
            lane = tx.lane(lane_id)
            lane["owner"] = "lane-owner-" + "9" * 32
            tx.save(lane)
        raise RuntimeError("lost ownership during call")

    service = make_service(model=_ScriptedModel([hijack_and_fail]))
    lane_id = service.spawn(
        command_id="spawn-hijack",
        parent_session_id="parent",
        task="hijack test",
        workspace_root=str(workspace),
        context=context,
    )["lane"]["id"]
    monkeypatch.setattr(
        service, "_loop_finish", lambda *args, **kwargs: finish_calls.append(args)
    )

    service.run_pending(lane_id, context)
    assert finish_calls == []


# ---------------------------------------------------------------------------
# sonder_runtime/interfaces/http/background_work.py
# ---------------------------------------------------------------------------


def test_background_work_sanitizes_non_finite_and_bool_numeric_fields():
    aggregator = BackgroundWorkAggregator(
        lanes=lambda context, **kwargs: {
            "lanes": [
                {
                    "id": "lane-bad-nums",
                    "task": "t",
                    "status": "running",
                    "updated_ts": float("inf"),
                    "started_ts": True,
                    "created_order": float("nan"),
                },
            ]
        },
        fleets=lambda **kwargs: [
            {
                "id": "master-bad",
                "role": "master",
                "status": "running",
                "requested_agents": float("inf"),
                "worker_slots": float("nan"),
                "started_ts": "inf",
                "updated_ts": True,
            },
        ],
        autopilot=lambda **kwargs: [
            {
                "id": "auto-bad",
                "status": "running",
                "current_task": True,
                "plan": [{"status": "running", "title": "first"}, {"status": "queued", "title": "second"}],
                "created_ts": float("inf"),
                "updated_ts": float("nan"),
            },
        ],
        owner_scope=lambda context: "owner:p",
        clock=lambda: float("inf"),
    )
    snap = aggregator.snapshot(SimpleNamespace(principal_id="p"))
    # Must be strictly valid JSON with no Infinity/NaN.
    encoded = json.dumps(snap, allow_nan=False)
    assert math.isfinite(snap["captured_at"])
    assert snap["groups"]["lanes"][0]["updated_ts"] == 0.0
    assert snap["groups"]["lanes"][0]["created_order"] == 0
    assert snap["groups"]["fleets"][0]["worker_slots"] == 0
    assert snap["groups"]["autopilot"][0]["current_task"] == ""
    assert "Infinity" not in encoded and "NaN" not in encoded


def test_background_work_bounds_oversized_provider_pages_children_and_strings():
    huge = "x" * 5000
    aggregator = BackgroundWorkAggregator(
        lanes=lambda context, **kwargs: {
            "lanes": [
                {"id": huge + str(i), "status": huge, "project": huge, "updated_ts": 100 + i}
                for i in range(10)
            ]
        },
        fleets=lambda **kwargs: [
            {"id": f"m-{i}", "role": "master", "status": "running", "started_ts": 100 + i}
            for i in range(5)
        ] + [
            {"id": f"c-{j}", "parent_id": "m-4", "status": "done", "updated_ts": 200 + j}
            for j in range(MAX_FLEET_CHILDREN + 15)
        ],
        autopilot=lambda **kwargs: [
            {
                "id": f"a-{i}",
                "status": "running",
                "phase": huge,
                "created_ts": 100 + i,
                "plan_json": "[" + ",".join('{"status":"done"}' for _ in range(20_000)) + "]",
            }
            for i in range(6)
        ],
        owner_scope=lambda context: "owner:p",
        clock=lambda: 500.0,
    )
    snap = aggregator.snapshot(SimpleNamespace(principal_id="p"), limit=2)

    assert len(snap["groups"]["lanes"]) == 2
    assert len(snap["groups"]["fleets"]) == 2
    assert len(snap["groups"]["autopilot"]) == 2
    assert snap["truncated"] == {"lanes": True, "fleets": True, "autopilot": True}

    lane = snap["groups"]["lanes"][0]
    assert len(lane["id"]) <= MAX_ID_CHARS
    assert len(lane["status"]) <= MAX_STATUS_CHARS
    assert len(lane["project"]) <= MAX_PROJECT_CHARS

    fleet = snap["groups"]["fleets"][0]
    assert fleet["id"] == "m-4"
    assert len(fleet["children"]) == MAX_FLEET_CHILDREN
    assert fleet["children_truncated"] is True
    assert fleet["counts"]["done"] == MAX_FLEET_CHILDREN + 15

    auto = snap["groups"]["autopilot"][0]
    assert len(auto["phase"]) <= MAX_PHASE_CHARS
    # Oversized plan_json (> 256 KiB) was safely skipped rather than decoded unboundedly.
    assert auto["task_counts"]["total"] == 0


def test_background_work_route_handles_malformed_query_edge_cases():
    seen = []

    def fleets(**kwargs):
        seen.append(kwargs)
        return []

    aggregator = BackgroundWorkAggregator(
        lanes=lambda context, **kwargs: {"lanes": []},
        fleets=fleets,
        autopilot=lambda **kwargs: [],
        owner_scope=lambda context: "owner:p",
        clock=lambda: 100.0,
    )
    ctx = SimpleNamespace(principal_id="p")

    # Empty lists and None project must not raise IndexError or stringify "None".
    ok = dispatch_background_work_route(
        aggregator,
        "GET",
        "/v1/background-work",
        context=ctx,
        query={"limit": [], "project": [None]},
    )
    assert ok is not None and ok.status_code == 200
    assert seen[-1]["project"] == ""
    assert seen[-1]["limit"] == 100

    for bad_query in (
        {"limit": "0"},
        {"limit": "-5"},
        {"limit": "abc"},
        {"limit": True},
        {"project": "x" * (MAX_PROJECT_CHARS + 1)},
        {"project": 123},
        "not-a-mapping",
    ):
        res = dispatch_background_work_route(
            aggregator, "GET", "/v1/background-work", context=ctx, query=bad_query
        )
        assert res is not None and res.status_code == 400
        assert res.body["error"] == "INVALID_INPUT"
