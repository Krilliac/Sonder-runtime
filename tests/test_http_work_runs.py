"""Routed HTTP work: wall-clock budget, cancel surface, and persisted answer."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.persistence import http_work_runs, session_repository
from sonder_runtime.adapters.web import lifecycle as sonder_lifecycle
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.interfaces.http import serve, work_runs
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread


LOCAL = {"mode": "local-open", "authorized": True}
ALICE = {"mode": "account", "authorized": True, "account": {"username": "alice", "role": "developer"}}
BOB = {"mode": "account", "authorized": True, "account": {"username": "bob", "role": "developer"}}


@pytest.fixture
def work_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(tmp_path / "runs.db"))
    http_work_runs.reset_for_tests()
    repository = session_repository.SQLiteSessionRepository(tmp_path / "sessions.sqlite")
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(session_repository=lambda: repository),
    )
    sonder_lifecycle.reset_for_tests()
    runner = work_runs.WorkRunner(store=http_work_runs, effects=effect_fence, wait_seconds=1, budget_seconds=60, max_running=1, thread_factory=owned_runtime_thread)
    monkeypatch.setattr(serve, "_WORK_RUNNER", runner)
    yield runner
    sonder_lifecycle.reset_for_tests()
    http_work_runs.reset_for_tests()


def _work(context=LOCAL, session="s-1"):
    return serve._handle_work_intent(
        "Build the Flutter app.", project="demo", authorized=True, context=context,
        session_id=session, session_ref=session, correlation_id="c-1", with_receipt=True,
    )


def _wait_finished(run_id, context=LOCAL):
    principal = serve._state_principal(context)
    for _ in range(200):
        record = serve._WORK_RUNNER.get(run_id, principal)
        if record and record["status"] != "running":
            return record
        threading.Event().wait(0.05)
    raise AssertionError("work run did not finish")


def test_slow_lane_answers_with_run_id_and_persists_the_answer(work_env, monkeypatch):
    release = threading.Event()

    def slow_lane(prompt, **_kwargs):
        release.wait(10)
        return "lane finished after the client stopped waiting"

    monkeypatch.setattr(server, "route_work_request", slow_lane)
    pending = _work()
    assert pending.status == "running"
    assert pending.work_run_id.startswith("wr-")
    assert pending.work_run_id in pending.text
    assert pending.public_receipt()["work_run_id"] == pending.work_run_id
    release.set()
    record = _wait_finished(pending.work_run_id)
    assert record["status"] == "returned"
    assert record["output"] == "lane finished after the client stopped waiting"


def test_fast_lane_returns_inline_with_run_id(work_env, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", lambda prompt, **_k: "done")
    result = _work()
    assert (result.status, result.text) == ("returned", "done")
    record = _wait_finished(result.work_run_id)
    assert record["output"] == "done"


def test_cancel_stops_effects_and_is_recorded(work_env, monkeypatch):
    started, cancelled, observed = threading.Event(), threading.Event(), {}

    def lane(prompt, **_kwargs):
        fence = effect_fence.current()
        observed["before"] = effect_fence.reason_lost(fence)
        started.set()
        cancelled.wait(10)
        # The permission gate asks this same fence before any effect.
        observed["after"] = effect_fence.reason_lost(fence)
        return "stopped"

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    assert started.wait(5)
    principal = serve._state_principal(LOCAL)
    assert serve._WORK_RUNNER.cancel(pending.work_run_id, principal)["cancel_requested"] is True
    cancelled.set()
    record = _wait_finished(pending.work_run_id)
    assert observed["before"] == ""
    assert "cancelled" in observed["after"]
    assert record["status"] == "cancelled"
    assert record["output"] == "stopped"


def test_wall_budget_expiry_fences_effects(work_env, monkeypatch):
    now = [1000.0]
    runner = work_runs.WorkRunner(store=http_work_runs, effects=effect_fence, wait_seconds=1, budget_seconds=60, max_running=1, thread_factory=owned_runtime_thread,
                                  clock=lambda: now[0])
    monkeypatch.setattr(serve, "_WORK_RUNNER", runner)
    observed = {}

    def lane(prompt, **_kwargs):
        now[0] += 61
        observed["lost"] = effect_fence.reason_lost(effect_fence.current())
        return "late"

    monkeypatch.setattr(server, "route_work_request", lane)
    result = _work()
    assert "wall-clock budget" in observed["lost"]
    assert _wait_finished(result.work_run_id)["status"] == "budget_exceeded"


def test_capacity_is_bounded(work_env, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server, "route_work_request",
                        lambda prompt, **_k: release.wait(10) and "ok")
    first = _work(session="s-a")
    assert first.status == "running"
    with pytest.raises(sonder_lifecycle.AdmissionRejected) as refused:
        _work(session="s-b")
    assert refused.value.status == 429
    assert refused.value.code == "WORK_CAPACITY_EXHAUSTED"
    release.set()
    _wait_finished(first.work_run_id)


def test_runs_are_owner_scoped(work_env, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", lambda prompt, **_k: "alice's answer")
    result = _work(context=ALICE)
    _wait_finished(result.work_run_id, ALICE)
    bob = serve._state_principal(BOB)
    assert serve._WORK_RUNNER.get(result.work_run_id, bob) is None
    assert serve._WORK_RUNNER.cancel(result.work_run_id, bob) is None
    assert serve._WORK_RUNNER.recent(bob) == []
    assert [r["id"] for r in serve._WORK_RUNNER.recent(serve._state_principal(ALICE))] == [
        result.work_run_id]


def test_restart_reconciles_orphaned_running_rows(work_env):
    http_work_runs.start("wr-" + "0" * 32, owner_scope="wo-x", process_id="proc-old",
                         deadline_ts=0)
    assert work_env.reconcile() == 1
    assert http_work_runs.get("wr-" + "0" * 32, owner_scope="wo-x")["status"] == "interrupted"


def test_work_run_http_routes(work_env, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", lambda prompt, **_k: "answer")
    result = _work()
    _wait_finished(result.work_run_id)
    sent = []
    handler = SimpleNamespace(
        _send_auth_error=lambda: sent.append(("auth", 401)),
        _send_json_payload=lambda payload, status=200, headers=None: sent.append((payload, status)),
        _request_auth_context=lambda: LOCAL,
    )
    assert serve.Handler._handle_work_run_request(handler, "GET", "/v1/work-runs/" + result.work_run_id)
    assert sent[-1][1] == 200 and sent[-1][0]["output"] == "answer"
    assert serve.Handler._handle_work_run_request(handler, "GET", "/v1/work-runs")
    assert sent[-1][0]["runs"][0]["id"] == result.work_run_id
    assert serve.Handler._handle_work_run_request(handler, "GET", "/v1/work-runs/wr-" + "f" * 32)
    assert sent[-1][1] == 404
    assert serve.Handler._handle_work_run_request(handler, "GET", "/v1/work-runs/../etc")
    assert sent[-1][1] == 400
    assert serve.Handler._handle_work_run_request(
        handler, "POST", "/v1/work-runs/%s/cancel" % result.work_run_id, context=LOCAL)
    assert sent[-1][1] == 200 and sent[-1][0]["status"] == "returned"
    assert not serve.Handler._handle_work_run_request(handler, "GET", "/v1/fanout")


def test_drain_fences_a_detached_run_and_counts_it_as_in_flight(work_env, monkeypatch):
    """A run that outlived its request must not keep changing things through a
    shutdown drain, and the drain must see it as an in-flight mutation."""
    runner = work_runs.WorkRunner(
        store=http_work_runs, effects=effect_fence, wait_seconds=1, budget_seconds=60,
        max_running=1, stop_reason=serve._work_run_stop_reason,
        lifetime=serve._work_run_lifetime,
        thread_factory=owned_runtime_thread,
    )
    monkeypatch.setattr(serve, "_WORK_RUNNER", runner)
    started, drained, observed = threading.Event(), threading.Event(), {}

    def lane(prompt, **_kwargs):
        fence = effect_fence.current()
        observed["before"] = effect_fence.reason_lost(fence)
        started.set()
        drained.wait(10)
        observed["after"] = effect_fence.reason_lost(fence)
        return "stopped by drain"

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    assert pending.status == "running"
    assert started.wait(5)
    coordinator = sonder_lifecycle.get().coordinator
    # The request has returned; only the detached run is still counted.
    assert coordinator.active_mutations == 1
    with coordinator._lock:
        coordinator._draining.set()
    drained.set()
    record = _wait_finished(pending.work_run_id)
    assert observed["before"] == ""
    assert "draining" in observed["after"]
    assert record["status"] == "interrupted"
    assert coordinator.active_mutations == 0
