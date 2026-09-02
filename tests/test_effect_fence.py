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
