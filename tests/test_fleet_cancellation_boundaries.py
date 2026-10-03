"""Cancellation before a call and late finishes must agree with durable state."""
from contextlib import contextmanager

import pytest

import master_orchestrator as mo
from sonder_runtime.adapters.persistence import fleet_store


def setup_function():
    mo.reset_for_tests()


@contextmanager
def _published_finishes(agent_id):
    received = []

    def receive(event):
        if event.source_id == agent_id and event.kind in (
            mo.events.FLEET_AGENT_DONE, mo.events.FLEET_AGENT_FAILED,
        ):
            received.append(event)

    mo.events.subscribe("*", receive)
    try:
        yield received
    finally:
        mo.events.unsubscribe("*", receive)


@pytest.mark.parametrize("provenance", [False, True], ids=["plain", "provenance"])
def test_inline_cancel_between_start_and_model_call_finishes_its_reservation(
    monkeypatch, tmp_path, provenance,
):
    task = "say hello"
    project = ""
    if provenance:
        (tmp_path / "scope.py").write_text("def target():\n    return 1\n", encoding="utf-8")
        task = "inspect target\n[objective:scope|file:scope.py|symbol:target]"
        project = str(tmp_path)
    original_begin = fleet_store.begin_model_call
    boundary = {}
    calls = []

    def cancel_before_begin(agent_id, *args, **kwargs):
        before = fleet_store.get_agent(agent_id)
        assert before["status"] == "running"
        assert not before["in_model_call"]
        assert mo.reserved_slot_count() == 1
        cancelled = mo.request_cancel(agent_id)
        assert cancelled["running"] == 1
        assert cancelled["model_calls"] == 0
        boundary["id"] = agent_id
        refused = original_begin(agent_id, *args, **kwargs)
        assert refused is None
        return refused

    monkeypatch.setattr(fleet_store, "begin_model_call", cancel_before_begin)
    result = mo.run_inline(
        task, lambda *args: calls.append(args) or "must never run", project=project,
    )

    assert result["output"] == "CANCELLED"
    assert result["master_id"] == boundary["id"]
    assert calls == []
    row = fleet_store.get_agent(result["master_id"])
    # Baseline RED: it returns CANCELLED but leaves a running, nonexecuting row.
    assert row["status"] == "cancelled"
    assert row["cancel_requested"] is True
    assert row["in_model_call"] is False
    assert row["finished_ts"] > 0
    assert row["output"] == ""
    assert mo.reserved_slot_count() == 0


@pytest.mark.parametrize("late_error", [False, True], ids=["late-output", "late-error"])
def test_cancelled_finish_does_not_publish_discarded_model_result(late_error):
    agent_id = mo._new_agent("agent", "call about to be cancelled")
    assert mo._start_agent(agent_id, "calling model", in_model_call=True)
    mo.request_cancel(agent_id)
    # Cancellation retains the actual active call's reservation until finish.
    assert mo.reserved_slot_count() == 1
    with _published_finishes(agent_id) as published:
        marker = mo._finish(
            agent_id, output="discarded late answer",
            error="discarded late transport error" if late_error else "",
        )

    assert marker == "CANCELLED"
    row = fleet_store.get_agent(agent_id)
    assert row["status"] == "cancelled"
    assert row["output"] == ""
    assert row["error"] == ""
    assert mo.reserved_slot_count() == 0
    assert len(published) == 1
    assert published[0].kind == mo.events.FLEET_AGENT_DONE
    assert published[0].data["result"] == row["output"]
    assert "discarded late" not in str(published[0].data)


@pytest.mark.parametrize("late_error", [False, True], ids=["late-output", "late-error"])
def test_duplicate_done_finish_publishes_the_persisted_result(late_error):
    agent_id = mo._new_agent("agent", "completed work")
    assert mo._start_agent(agent_id, "calling model", in_model_call=True)
    accepted_output = "accepted answer " * 50
    assert mo._finish(agent_id, output=accepted_output) == accepted_output
    before = fleet_store.get_agent(agent_id)
    with _published_finishes(agent_id) as published:
        marker = mo._finish(
            agent_id, output="unaccepted replacement",
            error="unaccepted late error" if late_error else "",
        )

    assert marker == accepted_output
    assert fleet_store.get_agent(agent_id) == before
    assert mo.reserved_slot_count() == 0
    assert len(published) == 1
    assert published[0].kind == mo.events.FLEET_AGENT_DONE
    assert published[0].data["result"] == accepted_output[:500]
    assert len(published[0].data["result"]) == 500
    assert "unaccepted" not in str(published[0].data)


def test_duplicate_failed_finish_publishes_the_persisted_failure():
    agent_id = mo._new_agent("agent", "failed work")
    assert mo._start_agent(agent_id, "calling model", in_model_call=True)
    mo._finish(agent_id, error="accepted failure")
    before = fleet_store.get_agent(agent_id)
    with _published_finishes(agent_id) as published:
        marker = mo._finish(agent_id, output="unaccepted replacement")

    assert marker == "ERROR: accepted failure"
    assert fleet_store.get_agent(agent_id) == before
    assert mo.reserved_slot_count() == 0
    assert len(published) == 1
    assert published[0].kind == mo.events.FLEET_AGENT_FAILED
    assert published[0].data["error"] == "accepted failure"


def test_missing_agent_finish_does_not_publish_invented_completion():
    agent_id = "missing-agent-for-finish-contract"
    assert fleet_store.get_agent(agent_id) is None
    with _published_finishes(agent_id) as published:
        marker = mo._finish(agent_id, output="invented completion")

    assert marker == ""
    assert published == []
    assert mo.reserved_slot_count() == 0
