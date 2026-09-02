"""An effect fence: a lost lease refuses effects at the next tool call.

Autopilot's record was already fenced (every progress write is conditional on
the worker still owning the run). The tool calls between those writes were
not. A worker now installs an effect fence for each task, and the permission
gate consults it before every effect-class decision on that thread: a lost
or cancelled run refuses every file change, host program and destructive
tool with ``source="fence"`` and a receipt, whatever the mode or rules say.
Reads are never fenced.
"""
from __future__ import annotations

import os
import time

import pytest

import permission_modes as pm
import server
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.security import permission_receipts

pytestmark = pytest.mark.unit


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    pm.reset_unattended_for_tests()
    yield
    pm.reset_unattended_for_tests()


def _lost(reason="lease gone"):
    return effect_fence.Fence("test", lambda: reason)


def _holding():
    return effect_fence.Fence("test", lambda: "")


def test_the_fence_is_installed_for_the_body_only():
    assert effect_fence.current() is None
    fence = _holding()
    with effect_fence.held(fence) as installed:
        assert installed is fence and effect_fence.current() is fence
        with effect_fence.held(_lost()) as inner:
            assert effect_fence.current() is inner
        assert effect_fence.current() is fence
    assert effect_fence.current() is None


def test_a_check_that_fails_means_the_fence_does_not_hold():
    def boom():
        raise RuntimeError("store unavailable")

    assert effect_fence.reason_lost(effect_fence.Fence("f", boom)).startswith(
        "the fence f could not be verified")
    assert effect_fence.reason_lost(_holding()) == ""
    assert effect_fence.reason_lost(None) == ""


def test_a_lost_fence_refuses_effects_whatever_the_mode_allows(gate):
    allowed = pm.decide("file_write", interactive=False, fence=_holding())
    assert allowed.action == pm.ALLOW and allowed.source == "mode"

    refused = pm.decide("file_write", interactive=False, fence=_lost("lease gone"))
    assert refused.action == pm.DENY and refused.source == "fence"
    assert "changes files" in refused.reason and "lease gone" in refused.reason
    assert pm.decide("run_code", interactive=False, fence=_lost()).source == "fence"

    with_rule = pm.decide(
        "file_write", interactive=False, fence=_lost(),
        rule_lookup=lambda tool: {"pattern": tool, "action": pm.ALLOW, "note": "x"},
    )
    assert with_rule.source == "fence", "a rule does not outrank a lost fence"


def test_reads_and_the_ask_class_are_never_fenced(gate):
    assert pm.decide("file_read", interactive=False, fence=_lost()).action == pm.ALLOW
    assert pm.decide("task_create", interactive=False, fence=_lost()).action == pm.ALLOW


def test_a_fence_refusal_leaves_a_receipt(gate):
    class _Sink:
        events = []

        def emit(self, code, *, summary, detail=None, severity="INFO", **_):
            self.events.append((code, dict(detail or {})))

    token = permission_receipts.snapshot()
    sink = _Sink()
    permission_receipts.install(lambda: sink)
    try:
        pm.decide("file_write", interactive=False, surface="agent", fence=_lost())
    finally:
        permission_receipts.restore(token)
    assert sink.events[-1][0] == permission_receipts.REFUSAL_EVENT
    assert sink.events[-1][1]["source"] == "fence"


def test_the_agent_gate_consults_the_fence_on_its_thread(gate):
    call = {"path": "a.txt", "content": "x"}
    assert server._agent_permission_gate_error("file_write", call) == ""
    with effect_fence.held(_lost("lease gone")):
        refusal = server._agent_permission_gate_error("file_write", call)
        assert refusal.startswith("ERROR: HOST POLICY: tool 'file_write' is refused because "
                                  "this worker's authority to produce effects is gone")
        assert "do not retry" in refusal
        assert server._agent_permission_gate_error("file_read", {"path": "a.txt"}) == ""
    assert server._agent_permission_gate_error("file_write", call) == ""


# --- the autopilot fence ---------------------------------------------------------------


@pytest.fixture
def autopilot_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _claimed(owner="owner-a"):
    run = autopilot_store.create_run("fence drill")
    claimed = autopilot_store.claim_run(run["id"], owner, owner_pid=os.getpid())
    assert claimed is not None and claimed["owner_id"] == owner
    return claimed


def test_the_autopilot_fence_holds_for_the_owner_and_breaks_with_the_lease(autopilot_db):
    run = _claimed()
    fence = effect_fence.autopilot_fence(run["id"], "owner-a")
    assert fence.label == "autopilot:%s" % run["id"]
    assert effect_fence.reason_lost(fence) == ""
    assert "no longer owned" in effect_fence.reason_lost(
        effect_fence.autopilot_fence(run["id"], "owner-b"))

    assert autopilot_store.reconcile_stale_runs(now=time.time() + 25_000) >= 1
    assert "no longer owned" in effect_fence.reason_lost(fence)


def test_the_autopilot_fence_breaks_on_cancel(autopilot_db):
    run = _claimed()
    fence = effect_fence.autopilot_fence(run["id"], "owner-a")
    assert autopilot_store.request_cancel(run["id"]) is not None
    assert "cancelled" in effect_fence.reason_lost(fence)


def test_the_worker_installs_the_fence_around_its_task(monkeypatch, autopilot_db):
    run = _claimed()
    seen = {}

    def fake_agent(prompt, **kwargs):
        seen["fence"] = effect_fence.current()
        return "done"

    monkeypatch.setattr(server, "_agent_impl", fake_agent)
    monkeypatch.setattr(server, "_autopilot_allowed_tools", lambda _run: frozenset({"file_read"}))
    monkeypatch.setattr(server, "_autopilot_tool_policy", lambda _run: None)
    task = {"id": "t1", "kind": "inspect", "title": "look", "instruction": "look"}
    assert server._autopilot_work_model(run, task, "") == "done"
    assert seen["fence"].label == "autopilot:%s" % run["id"]
    assert effect_fence.reason_lost(seen["fence"]) == ""
    assert effect_fence.current() is None


# --- the fleet and selfmod fences ---------------------------------------------------


def _fleet_row(agent_id):
    return {"id": agent_id, "role": "agent", "task": "work", "status": "queued",
            "activity": "queued", "started_ts": 100.0, "updated_ts": 100.0,
            "files": []}


@pytest.fixture
def fleet_db(monkeypatch, tmp_path):
    import sonder_runtime.adapters.persistence.fleet_store as fleet_store

    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    fleet_store.reset_schema_cache_for_tests()
    fleet_store.clear_all()
    yield fleet_store
    fleet_store.reset_schema_cache_for_tests()


def test_the_fleet_fence_holds_for_the_owner_and_breaks_on_reassignment_or_cancel(fleet_db):
    fleet_db.register_owner("owner-a", 101, 100.0)
    fleet_db.create_agent(_fleet_row("agent-1"), "owner-a", 101)
    fleet_db.start_agent("agent-1", "owner-a", "running", in_model_call=False, tool_calls=0)

    fence = effect_fence.fleet_fence("agent-1", "owner-a")
    assert fence.label == "fleet:agent-1"
    assert effect_fence.reason_lost(fence) == ""
    assert "no longer owned" in effect_fence.reason_lost(
        effect_fence.fleet_fence("agent-1", "owner-b"))
    assert "no longer owned" in effect_fence.reason_lost(
        effect_fence.fleet_fence("agent-missing", "owner-a"))

    fleet_db.cancel_agents("agent-1")
    assert "cancelled" in effect_fence.reason_lost(fence)


def test_the_fleet_fence_breaks_when_the_owner_heartbeat_expires(fleet_db):
    fleet_db.register_owner("owner-a", 101, 100.0)
    fleet_db.create_agent(_fleet_row("agent-2"), "owner-a", 101)
    fleet_db.start_agent("agent-2", "owner-a", "running", in_model_call=False, tool_calls=0)
    fence = effect_fence.fleet_fence("agent-2", "owner-a")
    assert effect_fence.reason_lost(fence) == ""
    # Reconcile as if every heartbeat is ancient: the agent is interrupted.
    fleet_db.reconcile_stale_owners(now=time.time() + 100_000)
    fleet_db.reconcile_stale_owners(now=time.time() + 200_000)
    assert effect_fence.reason_lost(fence) != ""


def test_the_fleet_worker_thread_is_fenced_while_bound(fleet_db, monkeypatch):
    import master_orchestrator

    monkeypatch.setattr(master_orchestrator, "_OWNER_ID", "owner-a")
    fleet_db.register_owner("owner-a", 101, 100.0)
    fleet_db.create_agent(_fleet_row("agent-3"), "owner-a", 101)
    with master_orchestrator._bind_worker_agent("agent-3"):
        fence = effect_fence.current()
        assert fence is not None and fence.label == "fleet:agent-3"
        assert effect_fence.reason_lost(fence) == ""
    assert effect_fence.current() is None


def test_the_selfmod_fence_follows_the_run_lease(monkeypatch):
    import sys
    import types

    runs = {}
    fake = types.ModuleType("selfmod")

    def get_run(run_id):
        try:
            return runs[run_id]
        except KeyError:
            raise KeyError("unknown selfmod run %s" % run_id)

    fake.get_run = get_run
    monkeypatch.setitem(sys.modules, "selfmod", fake)
    fence = effect_fence.selfmod_fence("run-1", "owner-a")
    assert fence.label == "selfmod:run-1"
    assert "no longer exists" in effect_fence.reason_lost(fence)

    runs["run-1"] = {"owner_id": "owner-a", "lease_until": time.time() + 600, "phase": "editing"}
    assert effect_fence.reason_lost(fence) == ""
    runs["run-1"]["lease_until"] = time.time() - 1
    assert "expired" in effect_fence.reason_lost(fence)
    runs["run-1"]["lease_until"] = time.time() + 600
    runs["run-1"]["owner_id"] = "owner-b"
    assert "no longer owned" in effect_fence.reason_lost(fence)
    runs["run-1"]["owner_id"] = "owner-a"
    runs["run-1"]["phase"] = "rejected"
    assert "is rejected" in effect_fence.reason_lost(fence)


def test_the_selfmod_editor_runs_under_the_run_fence(monkeypatch):
    seen = {}

    def fake_agent(prompt, **kwargs):
        seen["fence"] = effect_fence.current()
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_agent_impl", fake_agent)
    monkeypatch.setattr(server.selfmod, "get_run", lambda run_id: {
        "phase": "editing", "workspace_path": "/tmp/nowhere", "objective": "o",
        "evidence": [], "criteria": [], "files": [],
        "budgets": {"max_tool_calls": 3, "max_model_calls": 3},
    })
    monkeypatch.setattr(server.selfmod, "claim", lambda run_id: "owner-x")
    monkeypatch.setattr(server.selfmod, "heartbeat", lambda run_id, owner: True)
    monkeypatch.setattr(server.selfmod, "release", lambda run_id, owner: None)
    monkeypatch.setattr(server.selfmod, "record_reproducer_before", lambda run_id, cmd: None)
    monkeypatch.setattr(server.selfmod, "reject", lambda run_id, reason: None)
    monkeypatch.setattr(server, "_selfmod_test_commands", lambda run, explicit: [("a", ["x"]), ("b", ["y"])])
    monkeypatch.setattr(server, "_selfmod_agent_policy", lambda run: None)
    out = server._execute_selfmod_run("run-9")
    assert out.startswith("ERROR: selfmod run failed closed: stop here")
    assert seen["fence"].label == "selfmod:run-9"
    assert effect_fence.current() is None
