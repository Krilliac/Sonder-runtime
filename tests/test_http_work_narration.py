"""Focused transport tests for narrated HTTP work admission."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from sonder_runtime.bootstrap.http_work_narration import defer, deferred_post, response_scope, start_work
from sonder_runtime.interfaces.http.work_runs import WorkRunner


class _Store:
    def __init__(self):
        self.rows = {}

    def reconcile(self, _process): return 0
    def start(self, run_id, **kwargs): self.rows[run_id] = {"status": "running", **kwargs}
    def finish(self, run_id, status, output): self.rows[run_id]["status"] = status
    def cancel_requested(self, _run_id): return False
    def set_narration(self, run_id, **kwargs): self.rows[run_id].update(kwargs)


class _Fence:
    def __init__(self, _name, _check): pass


class _Effects:
    Fence = _Fence
    def held(self, _fence):
        from contextlib import nullcontext
        return nullcontext()
    def reason_lost(self, _fence): return ""


def test_zero_wait_defers_worker_until_response_scope_flushes():
    store, effects = _Store(), _Effects()
    called, done = [], threading.Event()
    runner = WorkRunner(store=store, effects=effects, wait_seconds=240,
                        thread_factory=threading.Thread)

    def work():
        called.append(True)
        done.set()

    with response_scope() as deferred:
        outcome = runner.run("owner", work,
                             classify=lambda result: ("returned", "ok"),
                             wait_seconds=0, defer_start=defer)
        assert outcome.finished is False
        assert called == []
        deferred.flush()
    assert done.wait(2)
    assert called == [True]


def test_default_wait_starts_worker_synchronously():
    store, effects = _Store(), _Effects()
    called = []
    runner = WorkRunner(store=store, effects=effects, wait_seconds=2,
                        thread_factory=threading.Thread)
    outcome = runner.run("owner", lambda: called.append(True),
                         classify=lambda result: ("returned", "ok"))
    assert outcome.finished is True
    assert called == [True]


class _InlineThread:
    def __init__(self, *, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_admission_is_persisted_and_http_ack_written_before_worker():
    events, store = [], _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=_InlineThread)

    @deferred_post
    def post(handler):
        receipt = start_work(runner, "alice", lambda: events.append("worker") or "result",
                             "I will inspect the project. watch status; cancel stop.", store,
                             classify=lambda value: ("returned", value))
        assert runner.running_count() == 1
        assert store.rows[receipt.work_run_id]["acknowledgement"] == receipt.text
        assert receipt.work_run_id in receipt.text
        events.append("assistant acknowledgement")
        return receipt

    receipt = post(SimpleNamespace())
    assert events == ["assistant acknowledgement", "worker"]
    assert runner.running_count() == 0
    assert store.rows[receipt.work_run_id]["status"] == "returned"


def test_deferred_flush_is_idempotent_and_preserves_context():
    from contextvars import ContextVar
    value = ContextVar("approved_context", default="none")
    calls, store = [], _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=_InlineThread)
    with response_scope() as deferred:
        token = value.set("approved exact call")
        runner.run("alice", lambda: calls.append(value.get()), classify=lambda _: ("returned", ""),
                   wait_seconds=0, defer_start=defer)
        value.reset(token)
        deferred.flush()
        deferred.flush()
    assert calls == ["approved exact call"]


def test_failed_ack_persistence_never_starts_work_or_retains_capacity():
    store = _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=_InlineThread)
    def unavailable(_run):
        raise OSError("unavailable")
    with pytest.raises(OSError, match="unavailable"):
        runner.run("alice", lambda: pytest.fail("started before durable acknowledgement"),
                   classify=lambda _: ("returned", ""), on_admitted=unavailable)
    assert runner.running_count() == 0
    assert [row["status"] for row in store.rows.values()] == ["failed"]


def test_deferred_thread_start_failure_terminalizes_and_releases_slot():
    class BrokenThread(_InlineThread):
        def start(self):
            raise RuntimeError("thread unavailable")
    store = _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=BrokenThread)
    with response_scope() as deferred:
        receipt = start_work(runner, "alice", lambda: "never", "Plan.", store,
                             classify=lambda _: ("returned", ""))
        with pytest.raises(RuntimeError, match="thread unavailable"):
            deferred.flush()
    assert store.rows[receipt.work_run_id]["status"] == "failed"
    assert runner.running_count() == 0


def test_work_status_projection_runs_only_after_owner_lookup():
    from sonder_runtime.interfaces.http.facades.work_runs import serve_request
    run_id = "wr-" + "1" * 32
    projected, sent = [], []
    row = {"id": run_id, "status": "returned", "output": "original", "custom": 7}
    runner = SimpleNamespace(get=lambda rid, owner: dict(row) if owner == "alice" else None)
    handler = SimpleNamespace(_send_json_payload=lambda payload, **kwargs: sent.append(payload))
    def project(record):
        projected.append(record["id"])
        return {**record, "progress": []}
    kwargs = dict(runner=runner, developer_authorized=lambda _: True,
                  principal_of=lambda ctx: ctx["owner"], store_errors=(OSError,), log=SimpleNamespace(),
                  status_projection=project)
    assert serve_request(handler, "GET", "/v1/work-runs/" + run_id, {"authorized": True, "owner": "bob"}, **kwargs)
    assert projected == []
    assert serve_request(handler, "GET", "/v1/work-runs/" + run_id, {"authorized": True, "owner": "alice"}, **kwargs)
    assert sent[-1] == {**row, "progress": []}


def test_terminal_provenance_is_preserved_on_original_work_run():
    from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult
    from sonder_runtime.interfaces.http.work_runs import current_run_id
    store = _Store()
    runner = WorkRunner(store=store, effects=_Effects(), thread_factory=_InlineThread)
    def work():
        return ChatWorkResult("done", "returned", work_run_id=current_run_id(),
                              source_event_id="source", return_event_id="returned")
    with response_scope() as deferred:
        receipt = start_work(runner, "alice", work, "Plan.", store,
                             classify=lambda result: (result.status, result.text))
        assert "result_receipt" not in store.rows[receipt.work_run_id]
        deferred.flush()
    final = store.rows[receipt.work_run_id]["result_receipt"]
    assert final["work_run_id"] == receipt.work_run_id
    assert final["source_event_id"] == "source" and final["return_event_id"] == "returned"
