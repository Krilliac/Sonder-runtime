"""Lane 4 (work-run lifecycle): cancel, budget, outcomes, approval parking, scope, narration."""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.local_observability import LocalObservabilitySink
from sonder_runtime.adapters.persistence import http_work_runs, session_repository
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.adapters.web import lifecycle as sonder_lifecycle
from sonder_runtime.application import foreground_turns
from sonder_runtime.application.context import bind_operation_context, local_owner_context
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.bootstrap import http_work_narration, work_narration
from sonder_runtime.domain.work_narration import acknowledgement
from sonder_runtime.interfaces.http import serve, work_runs
from sonder_runtime.interfaces.http.chat_tool_stream import notify_call_decision
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

LOCAL = {"mode": "local-open", "authorized": True}


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
    runner = work_runs.WorkRunner(
        store=http_work_runs, effects=effect_fence, wait_seconds=1, budget_seconds=60,
        max_running=1, thread_factory=owned_runtime_thread,
    )
    monkeypatch.setattr(serve, "_WORK_RUNNER", runner)
    runner.repository = repository
    yield runner
    # A failed approval test must not leave a run parked for its 600 s wait:
    # cancel whatever is still live, then wait for the registry to drain.
    principal = serve._state_principal(LOCAL)
    for run_id in list(runner._runs):
        with contextlib.suppress(Exception):
            runner.cancel(run_id, principal)
    end = time.monotonic() + 15
    while runner.running_count() and time.monotonic() < end:
        time.sleep(0.02)
    sonder_lifecycle.reset_for_tests()
    http_work_runs.reset_for_tests()


def _work(session="s-1"):
    return serve._handle_work_intent(
        "Build the Flutter app.", project="demo", authorized=True, context=LOCAL,
        session_id=session, session_ref=session, correlation_id="c-1", with_receipt=True,
    )


def _wait_finished(run_id, timeout=10):
    principal = serve._state_principal(LOCAL)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        record = serve._WORK_RUNNER.get(run_id, principal)
        if record and record["status"] != "running":
            return record
        time.sleep(0.02)
    raise AssertionError("work run did not finish")


def _wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _events(runner, session):
    return [e.event_type for e in runner.repository.read_complete(session, max_events=100)]


# (a) cancel mid-run -------------------------------------------------------

def test_cancel_stops_further_model_calls_and_releases_the_slot(work_env, monkeypatch):
    calls, started = [], threading.Event()

    def lane(prompt, **_kwargs):
        # The model-request boundary in server.py asks this exact function.
        for step in range(300):
            if foreground_turns.cancel_requested():
                return "stopped at the next boundary"
            calls.append(step)
            started.set()
            time.sleep(0.01)
        return "ran every step"

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    assert started.wait(5)
    serve._WORK_RUNNER.cancel(pending.work_run_id, serve._state_principal(LOCAL))
    record = _wait_finished(pending.work_run_id)
    assert record["status"] == "cancelled"
    assert len(calls) < 250, "model calls continued after cancel"
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) == settled
    # body() persists the terminal row before it pops the registry entry.
    _wait_for(lambda: serve._WORK_RUNNER.running_count() == 0)


# (b) wall budget ----------------------------------------------------------

def test_wall_budget_expiry_stops_at_the_next_step(work_env, monkeypatch):
    now = [1000.0]
    runner = work_runs.WorkRunner(
        store=http_work_runs, effects=effect_fence, wait_seconds=1, budget_seconds=60,
        max_running=1, thread_factory=owned_runtime_thread, clock=lambda: now[0],
    )
    runner.repository = work_env.repository
    monkeypatch.setattr(serve, "_WORK_RUNNER", runner)
    seen = {}

    def lane(prompt, **_kwargs):
        seen["before"] = foreground_turns.cancel_requested()
        now[0] += 61
        seen["after"] = foreground_turns.cancel_requested()
        return "late"

    monkeypatch.setattr(server, "route_work_request", lane)
    result = _work()
    assert _wait_finished(result.work_run_id)["status"] == "budget_exceeded"
    assert seen == {"before": False, "after": True}


def test_check_scope_is_local_to_its_context():
    assert foreground_turns.cancel_requested() is False
    with foreground_turns.check_scope(lambda: True):
        assert foreground_turns.cancel_requested() is True
    assert foreground_turns.cancel_requested() is False


# (c) outcome classification ----------------------------------------------

@pytest.mark.parametrize("output,status,event", [
    ("Refused: that is outside the project.", "refused", "chat.work.refused"),
    ("ERROR contacting worker http://node1: connection refused", "failed", "chat.work.failed"),
    ("EVIDENCE_REQUIRED: no host-observed tool evidence", "failed", "chat.work.failed"),
    ("ERROR: capacity busy, retry later", "busy", "chat.work.busy"),
    ("All done.", "returned", "chat.work.returned"),
])
def test_output_classifies_into_the_recorded_status(work_env, monkeypatch, output, status, event):
    monkeypatch.setattr(server, "route_work_request", lambda prompt, **_k: output)
    result = _work()
    record = _wait_finished(result.work_run_id)
    assert record["status"] == status
    assert record["output"] == output
    _wait_for(lambda: event in _events(work_env, "s-1"))
    assert record["result_receipt"]["status"] == status


# (d) approval parking -----------------------------------------------------

@pytest.fixture
def manual_ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved, loaded = dict(pm._STATE), pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.MANUAL, elevated=False, elevation_reason="")
    pm._LOADED = True
    pm.reset_unattended_for_tests()
    pm.risk_of("file_write")
    try:
        yield store
    finally:
        pm.forget_spent_approval()
        pm.reset_unattended_for_tests()
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = loaded


_WRITE = {"path": "notes.txt", "content": "hello", "mode": "create"}


def _gate_lane(prompt, **_kwargs):
    refusal = server._agent_permission_gate_error("file_write", dict(_WRITE))
    return "WROTE" if not refusal else refusal


def _awaiting(run_id):
    def look():
        row = serve._WORK_RUNNER.get(run_id, serve._state_principal(LOCAL))
        return row if row and row.get("awaiting_approval") else None
    return _wait_for(look)


def test_manual_mode_file_write_parks_then_approve_resumes(work_env, manual_ledger, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", _gate_lane)
    pending = _work()
    row = _awaiting(pending.work_run_id)
    call_id = row["awaiting_approval"]["call_id"]
    assert call_id and row["phase"] == "awaiting_approval"
    assert row["status"] == "running"
    assert row["pending_refusal"]["call_id"] == call_id
    # Parked: it holds no routed-work slot (the awaiting entry is published
    # just before the run is marked parked).
    _wait_for(lambda: work_env.slots_in_use() == 0)
    digest = pm.call_digest("file_write", _WRITE)
    manual_ledger.issue("file_write", digest, approver="operator")
    notify_call_decision(call_id)
    record = _wait_finished(pending.work_run_id)
    assert record["output"] == "WROTE"
    assert record["status"] == "returned"
    assert not record.get("awaiting_approval")
    assert record["pending_refusal"] is None


def test_manual_mode_deny_finishes_the_run(work_env, manual_ledger, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", _gate_lane)
    pending = _work()
    call_id = _awaiting(pending.work_run_id)["awaiting_approval"]["call_id"]
    manual_ledger.deny(call_id)
    notify_call_decision(call_id)
    record = _wait_finished(pending.work_run_id)
    assert record["output"] != "WROTE"
    assert record["status"] == "failed"
    assert "refused by the active permission gate" in record["output"]
    assert not record.get("awaiting_approval")
    _wait_for(lambda: work_env.running_count() == 0)


def test_cancel_while_parked_ends_the_wait(work_env, manual_ledger, monkeypatch):
    monkeypatch.setattr(server, "route_work_request", _gate_lane)
    pending = _work()
    _awaiting(pending.work_run_id)
    serve._WORK_RUNNER.cancel(pending.work_run_id, serve._state_principal(LOCAL))
    assert _wait_finished(pending.work_run_id)["status"] == "cancelled"


# (e) session mode ----------------------------------------------------------

def test_session_mode_scope_is_kept_in_the_body_thread(work_env, monkeypatch):
    seen = {}

    def lane(prompt, **_kwargs):
        seen["mode"] = pm.current_mode()
        return "ok"

    monkeypatch.setattr(server, "route_work_request", lane)
    monkeypatch.setitem(pm._SCOPED_MODES, pm._scope_key("owner", "sess-A"), pm.MANUAL)
    monkeypatch.setitem(pm._SCOPED_MODES, pm._scope_key("owner", "sess-B"), pm.AUTO)
    with pm.mode_scope("owner", "sess-A"):
        started = _work(session="sess-A")
    _wait_finished(started.work_run_id)
    assert seen["mode"] == pm.MANUAL


def test_runner_reenters_the_admission_scope_in_the_body(work_env):
    """The body must not depend on the worker's inherited context copy."""
    seen = {}
    entered = []

    class _Scope:
        def __enter__(self):
            entered.append("in")
            return self

        def __exit__(self, *exc):
            entered.append("out")
            return False

    done = threading.Event()

    def call():
        seen["inside"] = list(entered)
        done.set()
        return "ok"

    work_env.run("local", call, classify=lambda r: ("returned", str(r)), wait_seconds=0,
                 body_scope=lambda handle: _Scope())
    assert done.wait(5)
    assert seen["inside"] == ["in"]


# (f) recent() enrichment ---------------------------------------------------

def test_recent_row_enriches_to_result_is_available(work_env):
    http_work_runs.start("wr-recent", owner_scope="wo-r", process_id="p", deadline_ts=1)
    http_work_runs.finish("wr-recent", "returned", "the answer")
    [row] = http_work_runs.recent(owner_scope="wo-r")
    assert "output" not in row and row["has_output"] is True
    assert "Result is available" in work_narration.enrich_work_record(row)["final_summary"]
    http_work_runs.start("wr-empty", owner_scope="wo-e", process_id="p", deadline_ts=1)
    http_work_runs.finish("wr-empty", "returned", "")
    [empty] = http_work_runs.recent(owner_scope="wo-e")
    assert "No output" in work_narration.enrich_work_record(empty)["final_summary"]


# (g) goal truncation ---------------------------------------------------------

def test_long_goal_is_cut_at_a_word_with_ellipsis():
    words = ["alphabetic%03d" % i for i in range(30)]
    goal = " ".join(words)[:207]
    assert len(goal) == 207
    text = acknowledgement(goal=goal, mode="workbench")
    shown = text.split("I will work on: ", 1)[1].split(". Mode:", 1)[0]
    assert shown.endswith("...")
    body = shown[:-3]
    assert goal.startswith(body) and goal[len(body)] == " "
    assert len(shown) <= 180
    short = acknowledgement(goal="fix the bug", mode="workbench")
    assert "I will work on: fix the bug. Mode:" in short


# (h) correlation id ------------------------------------------------------------

def test_sink_defaults_correlation_to_the_bound_operation_context():
    sink = LocalObservabilitySink(clock=lambda: 1.0, redactor=Redactor(env={}))
    context = local_owner_context(correlation_id="req-42", source="http")
    with bind_operation_context(context):
        sink.emit("PERMISSION_CHECK", summary="s", detail={"category": "permission"})
        sink.emit("PERMISSION_CHECK", summary="s", detail={"category": "permission"},
                  correlation_id="explicit-1")
    sink.emit("PERMISSION_CHECK", summary="s", detail={"category": "permission"})
    rows = {row["sequence"]: row["correlation_id"] for row in sink.recent_events(10)}
    assert rows[1] == "req-42"
    assert rows[2] == "explicit-1"
    assert rows[3].startswith("local-")


# MA-14 (b) delegated -------------------------------------------------------------

def test_a_lane_that_links_a_child_run_finishes_delegated(work_env, monkeypatch):
    def lane(prompt, **_kwargs):
        http_work_runs.link_run(work_runs.current_run_id(), "autopilot", "ap-child-1")
        return "Autopilot started as ap-child-1."

    monkeypatch.setattr(server, "route_work_request", lane)
    result = _work()
    record = _wait_finished(result.work_run_id)
    assert record["status"] == "delegated"
    assert record["result_receipt"]["child_run_id"] == "ap-child-1"
    _wait_for(lambda: "chat.work.delegated" in _events(work_env, "s-1"))


# schema migration ---------------------------------------------------------------------

def test_an_older_database_gains_the_new_statuses(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(path))
    old = ", ".join("'%s'" % s for s in (
        "running", "returned", "unknown", "refused", "cancelled",
        "budget_exceeded", "interrupted", "failed"))
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE http_work_runs (run_id TEXT PRIMARY KEY, owner_scope TEXT NOT NULL, "
        "process_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN (%s)), "
        "created_ts REAL NOT NULL, updated_ts REAL NOT NULL, deadline_ts REAL NOT NULL, "
        "cancel_requested INTEGER NOT NULL DEFAULT 0, output TEXT NOT NULL DEFAULT '', "
        "output_truncated INTEGER NOT NULL DEFAULT 0, narration TEXT NOT NULL DEFAULT '{}');"
        "INSERT INTO http_work_runs(run_id, owner_scope, process_id, status, created_ts, "
        "updated_ts, deadline_ts, output) VALUES ('wr-old', 'wo-x', 'p', 'returned', 1, 1, 1, 'kept');" % old
    )
    conn.commit()
    conn.close()
    http_work_runs.reset_for_tests()
    try:
        assert http_work_runs.get("wr-old", owner_scope="wo-x")["output"] == "kept"
        http_work_runs.start("wr-new", owner_scope="wo-x", process_id="p", deadline_ts=1)
        http_work_runs.finish("wr-new", "busy", "later")
        http_work_runs.start("wr-new2", owner_scope="wo-x", process_id="p", deadline_ts=1)
        http_work_runs.finish("wr-new2", "delegated", "child")
        assert http_work_runs.get("wr-new", owner_scope="wo-x")["status"] == "busy"
        assert http_work_runs.get("wr-new2", owner_scope="wo-x")["status"] == "delegated"
    finally:
        http_work_runs.reset_for_tests()


# (f) slot release and re-admission while parked ----------------------------

def _start_parking_run(runner, name, handles, events, release):
    """A run whose body parks until ``release`` is set, then records ``<name>-continued``."""
    entered = threading.Event()

    def call():
        with handles[name].parked():
            entered.set()
            release.wait(10)
        events.append(name + "-continued")
        return "ok"

    def scope(handle):
        handles[name] = handle
        return contextlib.nullcontext()

    outcome = runner.run(serve._state_principal(LOCAL), call, classify=lambda r: ("returned", str(r)),
                         wait_seconds=0, body_scope=scope)
    assert entered.wait(5)
    return outcome.run_id


def _start_holding_run(runner, events, release):
    started = threading.Event()

    def call():
        started.set()
        release.wait(10)
        events.append("B-finished")
        return "ok"

    outcome = runner.run(serve._state_principal(LOCAL), call, classify=lambda r: ("returned", str(r)), wait_seconds=0)
    assert started.wait(5)
    return outcome.run_id


def test_a_parked_run_frees_its_slot_for_another_run(work_env):
    handles, events = {}, []
    release_a, release_b = threading.Event(), threading.Event()
    run_a = _start_parking_run(work_env, "A", handles, events, release_a)
    assert work_env.slots_in_use() == 0
    run_b = _start_holding_run(work_env, events, release_b)  # must not raise WorkCapacityExhausted
    assert work_env.slots_in_use() == 1
    with pytest.raises(work_runs.WorkCapacityExhausted):
        work_env.run(serve._state_principal(LOCAL), lambda: "x", classify=lambda r: ("returned", "x"), wait_seconds=0)
    release_a.set()
    release_b.set()
    _wait_finished(run_a)
    _wait_finished(run_b)


def test_an_approved_run_waits_for_a_free_slot_before_continuing(work_env):
    handles, events = {}, []
    release_a, release_b = threading.Event(), threading.Event()
    run_a = _start_parking_run(work_env, "A", handles, events, release_a)
    run_b = _start_holding_run(work_env, events, release_b)
    release_a.set()  # the operator approved while B still holds the only slot
    time.sleep(0.4)
    assert "A-continued" not in events
    assert work_env.slots_in_use() == 1
    release_b.set()
    _wait_finished(run_b)
    assert _wait_finished(run_a)["status"] == "returned"
    assert events.index("B-finished") < events.index("A-continued")


def test_cancel_ends_the_re_admission_wait(work_env):
    handles, events = {}, []
    release_a, release_b = threading.Event(), threading.Event()
    run_a = _start_parking_run(work_env, "A", handles, events, release_a)
    run_b = _start_holding_run(work_env, events, release_b)
    release_a.set()
    time.sleep(0.3)
    assert "A-continued" not in events, "re-admission did not wait for the slot"
    work_env.cancel(run_a, serve._state_principal(LOCAL))
    assert _wait_finished(run_a, timeout=5)["status"] == "cancelled"
    assert "B-finished" not in events  # the wait ended with B still holding the slot
    release_b.set()
    _wait_finished(run_b)


# (g) capacity refusals the pool really produces are 'busy' -------------------

@pytest.mark.parametrize("error", [
    ollama_pool.WorkerPoolBackpressure("Ollama worker queue is full"),
    ollama_pool.WorkerPoolBackpressure("timed out waiting for Ollama worker capacity"),
])
def test_pool_refusal_reply_classifies_as_busy(error):
    reply = ollama_pool.pool_refusal_reply(error)
    assert reply and http_work_narration.classify_output(reply) == "busy"


@pytest.mark.parametrize("text", [
    "ERROR: timed out waiting for Ollama worker capacity",
    "ERROR: Ollama worker queue is full",
])
def test_raw_capacity_wording_classifies_as_busy(text):
    assert http_work_narration.classify_output(text) == "busy"


def test_a_draining_pool_reply_is_not_busy():
    reply = ollama_pool.pool_refusal_reply(ollama_pool.WorkerPoolDraining("drain"))
    assert http_work_narration.classify_output(reply) == "failed"


# (h) approval parking across threads ---------------------------------------

def test_offthread_parkers_do_not_release_the_slot(work_env):
    import contextvars
    handles, yielded = {}, []
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    sibling = threading.Event()

    def child(i):
        with handles["h"].parked() as released:
            yielded.append(released)
            entered[i].set()
            release[i].wait(10)

    def call():
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(child, i)) for i in (0, 1)]
        threads.append(threading.Thread(target=lambda: sibling.wait(10)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return "ok"

    def scope(handle):
        handles["h"] = handle
        return contextlib.nullcontext()

    outcome = work_env.run(serve._state_principal(LOCAL), call, classify=lambda r: ("returned", str(r)),
                           wait_seconds=0, body_scope=scope)
    try:
        assert entered[0].wait(5) and entered[1].wait(5)
        assert yielded == [False, False]
        assert work_env.slots_in_use() == 1
        with pytest.raises(work_runs.WorkCapacityExhausted):
            work_env.run(serve._state_principal(LOCAL), lambda: "x",
                         classify=lambda r: ("returned", "x"), wait_seconds=0)
        release[0].set()
        time.sleep(0.3)
        assert work_env.slots_in_use() == 1
    finally:
        release[0].set()
        release[1].set()
        sibling.set()
    _wait_finished(outcome.run_id)


def test_offthread_exit_does_not_unpark_the_owner(work_env):
    handles = {}
    state = {}
    owner_in, other_done, owner_go = threading.Event(), threading.Event(), threading.Event()
    inner_done = threading.Event()

    def other():
        with handles["h"].parked() as released:
            state["other"] = released
        other_done.set()

    def call():
        with handles["h"].parked() as outer:
            state["outer"] = outer
            with handles["h"].parked() as inner:
                state["inner"] = inner
                owner_in.set()
                other_done.wait(10)
            state["slots_after_inner"] = work_env.slots_in_use()
            inner_done.set()
            owner_go.wait(10)
        return "ok"

    def scope(handle):
        handles["h"] = handle
        return contextlib.nullcontext()

    outcome = work_env.run(serve._state_principal(LOCAL), call, classify=lambda r: ("returned", str(r)),
                           wait_seconds=0, body_scope=scope)
    try:
        assert owner_in.wait(5)
        assert work_env.slots_in_use() == 0
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        assert other_done.is_set()
        assert state["other"] is False
        assert inner_done.wait(5)
        # The off-thread exit and the nested owner exit both left the run parked.
        assert state["outer"] is True and state["inner"] is True
        assert state["slots_after_inner"] == 0
        assert work_env.slots_in_use() == 0
    finally:
        owner_go.set()
    _wait_finished(outcome.run_id)
    _wait_for(lambda: work_env.slots_in_use() == 0)


def test_awaiting_approvals_are_keyed_by_call_id(work_env):
    principal = serve._state_principal(LOCAL)
    gate = threading.Event()
    outcome = work_env.run(principal, lambda: gate.wait(10) and "ok",
                           classify=lambda r: ("returned", "ok"), wait_seconds=0)
    run_id = outcome.run_id

    def row():
        return http_work_runs.get(run_id, owner_scope=work_runs.owner_scope(principal))

    try:
        for index, call_id in enumerate(("c1", "c2")):
            http_work_runs.set_narration(run_id, awaiting_approval={
                "call_id": call_id, "tool": "file_write", "since": "%d.0" % (100 + index),
                "wait_seconds": 600})
        current = row()
        assert [item["call_id"] for item in current["awaiting_approvals"]] == ["c1", "c2"]
        assert current["awaiting_approval"]["call_id"] == "c1"
        assert current["phase"] == "awaiting_approval"
        http_work_runs.set_narration(run_id, clear_awaiting_approval="c1")
        current = row()
        assert current["awaiting_approval"]["call_id"] == "c2"
        assert [item["call_id"] for item in current["awaiting_approvals"]] == ["c2"]
        assert current["phase"] == "awaiting_approval"
        http_work_runs.set_narration(run_id, clear_awaiting_approval="c2")
        current = row()
        assert current["awaiting_approval"] is None
        assert current["awaiting_approvals"] == []
        assert current["phase"] == "running"
        http_work_runs.set_narration(run_id, awaiting_approval={"call_id": "c3", "tool": "file_write"})
    finally:
        gate.set()
    _wait_finished(run_id, timeout=10)
    current = row()
    assert "awaiting_approval" not in current["narration"]
    assert "awaiting_approvals" not in current["narration"]
    assert current["awaiting_approvals"] == [] and current["awaiting_approval"] is None


def test_two_concurrent_approval_waits_on_one_run(work_env, manual_ledger, monkeypatch):
    import contextvars
    results = {}
    paths = ("a.txt", "b.txt")

    def gate(path):
        results[path] = server._agent_permission_gate_error("file_write", dict(_WRITE, path=path))

    def lane(prompt, **_kwargs):
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(gate, path)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return "WROTE" if not any(results.values()) else "refused: " + "|".join(v for v in results.values() if v)

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    principal = serve._state_principal(LOCAL)

    def both():
        row = serve._WORK_RUNNER.get(pending.work_run_id, principal)
        return row if row and len(row.get("awaiting_approvals") or []) == 2 else None

    row = _wait_for(both)
    assert work_env.slots_in_use() == 1
    by_path = {}
    for item in row["awaiting_approvals"]:
        digest = manual_ledger.resolve_call(item["call_id"]).digest
        for path in paths:
            if digest == pm.call_digest("file_write", dict(_WRITE, path=path)):
                by_path[path] = item["call_id"]
    assert sorted(by_path) == sorted(paths)
    first, second = paths
    manual_ledger.issue("file_write", pm.call_digest("file_write", dict(_WRITE, path=first)), approver="operator")
    notify_call_decision(by_path[first])
    _wait_for(lambda: first in results)
    assert results[first] == ""
    row = serve._WORK_RUNNER.get(pending.work_run_id, principal)
    assert [item["call_id"] for item in row["awaiting_approvals"]] == [by_path[second]]
    assert row["awaiting_approval"]["call_id"] == by_path[second]
    assert row["phase"] == "awaiting_approval"
    assert work_env.slots_in_use() == 1
    manual_ledger.deny(by_path[second])
    notify_call_decision(by_path[second])
    record = _wait_finished(pending.work_run_id)
    assert results[second]
    assert record["awaiting_approvals"] == [] and record["awaiting_approval"] is None


def test_delegated_child_depth_is_not_relayed(work_env, manual_ledger, monkeypatch):
    from sonder_runtime.application.agents import delegation_runtime

    def lane(prompt, **_kwargs):
        with delegation_runtime.depth_scope(1):
            refusal = server._agent_permission_gate_error("file_write", dict(_WRITE))
        return "WROTE" if not refusal else refusal

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    principal = serve._state_principal(LOCAL)
    seen = []
    record = None
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            record = serve._WORK_RUNNER.get(pending.work_run_id, principal)
            if record and record.get("awaiting_approval"):
                seen.append(record["awaiting_approval"])
            if record and record["status"] != "running":
                break
            time.sleep(0.02)
        assert record and record["status"] != "running", "child gate parked instead of refusing"
    finally:
        serve._WORK_RUNNER.cancel(pending.work_run_id, principal)
    assert record["output"] != "WROTE"
    assert not seen


def test_late_awaiting_entry_is_not_stored_on_a_finished_run(work_env):
    principal = serve._state_principal(LOCAL)
    outcome = work_env.run(principal, lambda: "ok", classify=lambda r: ("returned", "ok"), wait_seconds=5)
    run_id = outcome.run_id
    _wait_finished(run_id)
    http_work_runs.set_narration(run_id, awaiting_approval={"call_id": "late", "tool": "file_write"})
    row = http_work_runs.get(run_id, owner_scope=work_runs.owner_scope(principal))
    assert "awaiting_approvals" not in row["narration"]
    assert "awaiting_approval" not in row["narration"]
    assert row["awaiting_approvals"] == []


def test_real_delegation_executor_children_are_refused_not_parked(work_env, manual_ledger, monkeypatch):
    from sonder_runtime.application.agents import delegation_runtime
    from sonder_runtime.bootstrap import delegation_wiring
    from sonder_runtime.domain.agents.delegation import DelegationTaskSpec

    refusals = []
    fake_server = SimpleNamespace(
        master_orchestrator=SimpleNamespace(capacity=lambda **_kw: {"worker_slots": 2}),
        OLLAMA_POOL=None,
        BASE="",
        _agent_impl=lambda prompt, **_kw: (
            refusals.append(server._agent_permission_gate_error("file_write", dict(_WRITE))) or
            "refused: " + refusals[-1]
        ),
    )
    executor = delegation_wiring.make_delegation_executor(fake_server)
    specs = [DelegationTaskSpec(task="write a", agent_id="child-a"),
             DelegationTaskSpec(task="write b", agent_id="child-b")]
    slots = []
    results = []

    def lane(prompt, **_kwargs):
        board = delegation_runtime.AgentProgressBoard()
        results.extend(executor(specs, parent_agent_id="coordinator", parent_project="",
                                parent_read_only=False, board=board))
        slots.append(work_env.slots_in_use())
        return "done"

    monkeypatch.setattr(server, "route_work_request", lane)
    pending = _work()
    principal = serve._state_principal(LOCAL)
    seen = []
    end = time.monotonic() + 10
    record = None
    while time.monotonic() < end:
        record = serve._WORK_RUNNER.get(pending.work_run_id, principal)
        if record and record.get("awaiting_approval"):
            seen.append(record["awaiting_approval"])
        if record and record["status"] != "running":
            break
        time.sleep(0.02)
    assert record and record["status"] != "running", "a delegated child parked instead of refusing"
    assert not seen
    assert len(results) == 2 and len(refusals) == 2 and all(refusals)
    assert slots == [1]

def test_live_runs_are_capped_even_when_they_are_parked(work_env):
    """Parking frees a running slot, never the total-live bound."""
    work_env.configure(wait_seconds=1, budget_seconds=60, max_running=1, max_live=3)
    handles, events = {}, []
    release = threading.Event()
    run_ids = [_start_parking_run(work_env, "P%d" % i, handles, events, release) for i in range(3)]
    try:
        _wait_for(lambda: work_env.slots_in_use() == 0)
        assert work_env.running_count() == 3
        with pytest.raises(work_runs.WorkCapacityExhausted):
            work_env.run(serve._state_principal(LOCAL), lambda: "x",
                         classify=lambda r: ("returned", "x"), wait_seconds=0)
        assert work_env.running_count() == 3
    finally:
        release.set()
    for run_id in run_ids:
        _wait_finished(run_id)
    _wait_for(lambda: work_env.running_count() == 0)
    # Capacity returns once the parked runs are gone.
    again = work_env.run(serve._state_principal(LOCAL), lambda: "x",
                         classify=lambda r: ("returned", "x"), wait_seconds=5)
    assert again.finished

