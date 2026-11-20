"""Lane agent-outcomes: how an agent run, a fleet worker and Autopilot report
a model failure (MA-18, F15, MA-06, MA-04, MA-10, and the F06 cancel check).

Every test here drives the real code path through a seam a caller could use;
none of them pins an implementation detail that a rewrite could keep passing
while the behaviour regressed.
"""
from __future__ import annotations

import os

import pytest

import autopilot_controller
import autopilot_store
import calibration
import master_orchestrator
import server
import sonder_runtime.adapters.agent_outcomes as agent_outcomes
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.model_transport import ModelCallError


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(
        server, "prepare_writing_project", lambda project, task="": (project, ""),
    )
    activity_tracker.reset_for_tests()


class _Conn:
    def close(self):
        pass


def _poor_record(monkeypatch):
    """A measured-poor record, so the verification standing is demanded."""
    monkeypatch.setattr(server, "_open_db", lambda: _Conn())
    monkeypatch.setattr(server, "_open_db_readonly", lambda: _Conn())
    monkeypatch.setattr(
        calibration, "_counts", lambda *a, **k: {"accepted": 40, "rejected": 60},
    )


def _scripted(monkeypatch, responses, observations=()):
    replies = list(responses)
    obs = iter(list(observations))
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda prompt, history=None: replies.pop(0)),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *a, **k: next(obs),
    )


# --- (a) MA-18: an ERROR exit is not "complete" ------------------------------


def test_repeat_guard_error_exit_reports_incomplete(monkeypatch):
    _scripted(
        monkeypatch,
        ['{"tool": "script_run", "args": {"path": "missing.py"}}'] * 4,
        ["ERROR: missing.py"] * 4,
    )
    with activity_tracker.response_span("agent:test", "run the script"):
        output = server._agent_impl("run the script", max_steps=4)
        report = activity_tracker.format_end_report()

    assert output.startswith("ERROR: agent repeated the same unsuccessful tool call")
    latest = activity_tracker.latest()
    assert latest["status"] == "incomplete"
    assert "result: incomplete" in report
    assert "result: complete" not in report


def test_model_final_that_is_an_error_reports_incomplete(monkeypatch):
    _scripted(monkeypatch, ['{"final":"ERROR: could not finish the task"}'])
    with activity_tracker.response_span("agent:test", "do the work"):
        output = server._agent_impl("do the work", max_steps=1)
        inside = activity_tracker.current()["status"]

    assert output.startswith("ERROR: could not finish")
    assert inside == "incomplete"
    assert activity_tracker.latest()["status"] == "incomplete"


# --- (b) F15: a read-only run is not UNVERIFIED and not "Implement" ---------


def test_non_mutating_run_that_ran_a_failing_test_has_no_unverified_banner(monkeypatch):
    _poor_record(monkeypatch)
    _scripted(
        monkeypatch,
        ['{"tool":"test_run","args":{"root":"."}}', '{"final":"2 tests failed"}'],
        ["test run (pytest)\n  ok: False\n  returncode: 1\n  2 failed"],
    )
    with activity_tracker.response_span("agent:test", "run the tests"):
        output = server._agent_impl("run the tests", max_steps=2)
        inside = activity_tracker.current()["status"]

    assert server._AGENT_UNVERIFIED_PREFIX not in output
    assert "2 tests failed" in output
    assert inside != "unverified"
    assert activity_tracker.latest()["status"] != "unverified"


def test_non_mutating_run_with_no_verifier_has_no_unverified_banner(monkeypatch):
    _poor_record(monkeypatch)
    _scripted(monkeypatch, ['{"final":"the answer is 42"}'])
    with activity_tracker.response_span("agent:test", "explain the module"):
        output = server._agent_impl("explain the module", max_steps=1)
        inside = activity_tracker.current()["status"]

    assert server._AGENT_UNVERIFIED_PREFIX not in output
    assert "the answer is 42" in output
    assert inside != "unverified"


def test_mutating_run_without_verification_still_gets_the_banner(monkeypatch, tmp_path):
    _poor_record(monkeypatch)
    _scripted(
        monkeypatch,
        [
            '{"tool":"project_detect","args":{"path":"."}}',
            '{"tool":"file_write","args":{"path":"%s","content":"x","mode":"overwrite"}}'
            % (tmp_path / "a.py").as_posix(),
            '{"final":"done"}',
        ],
        ['{"root":".","manifests":[{"path":"pyproject.toml"}],"errors":[]}', "wrote 1 byte"],
    )
    output = server._agent_impl("write a file", max_steps=3)

    assert output.startswith(server._AGENT_UNVERIFIED_PREFIX)


def test_vacuous_run_is_still_noted_for_the_escalating_runner(monkeypatch):
    _poor_record(monkeypatch)
    server._take_agent_model_failure()
    _scripted(monkeypatch, ['{"final":"all done"}'])
    server._agent_impl("fix the bug", max_steps=1)

    noted = server._take_agent_model_failure()
    assert noted is not None and noted["vacuous"] is True


def test_checklist_item_two_is_neutral(monkeypatch):
    captured = {}

    def create(title, items_json, project="", owner="agent", priority=1):
        captured["items"] = items_json
        return "created sonder checklist abc123"

    monkeypatch.setattr(server, "checklist_create", create)
    monkeypatch.setattr(server, "checklist_update", lambda *a, **k: "ok")
    server._start_agent_checklist("run the tests", "", False)

    assert "Implement" not in captured["items"]
    assert "Carry out the requested work" in captured["items"]


# --- (c) MA-06 / MA-04: pool contention is a capacity failure ---------------


def _backpressure_receipt(monkeypatch, tmp_path):
    def refuse(*_args, **_kwargs):
        raise ollama_pool.WorkerPoolBackpressure("Ollama worker queue is full")

    monkeypatch.setattr(server, "_post", refuse)
    return server._agent_impl(
        "inspect the repository", max_steps=1, project=str(tmp_path),
        return_host_receipt=True,
    )


def test_receipt_of_a_model_failure_carries_its_kind(monkeypatch, tmp_path):
    receipt = _backpressure_receipt(monkeypatch, tmp_path)

    assert isinstance(receipt, autopilot_controller.HostTaskResult)
    assert receipt.model_failure_kind in {"transport", "timeout"}
    assert receipt.model_failure_transient is True


def test_repository_worker_result_reraises_a_classified_model_failure(monkeypatch, tmp_path):
    receipt = _backpressure_receipt(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as caught:
        master_orchestrator.repository_worker_result(receipt, str(tmp_path))

    assert master_orchestrator.classify_worker_error(caught.value) in (
        master_orchestrator.TRANSIENT_FAILURE_CLASSES
    )
    assert "ERROR" in str(caught.value)
    assert "no host-observed file evidence" not in str(caught.value)


def test_receipt_with_evidence_and_a_model_failure_kind_is_kept(tmp_path):
    receipt = autopilot_controller.HostTaskResult(
        output=(
            "ERROR contacting the model pool: timed out\n\n"
            "=== TOOL EVIDENCE ===\nfile_read ok"
        ),
        project_scope=str(tmp_path),
        tools=("file_read",),
        model_failure_kind="timeout",
        model_failure_transient=True,
    )

    result = master_orchestrator.repository_worker_result(receipt, str(tmp_path))

    assert isinstance(result, master_orchestrator.RepositoryWorkerResult)
    assert "file_read" in result.tools


def test_scope_mismatch_is_not_reported_as_a_capacity_failure(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    receipt = autopilot_controller.HostTaskResult(
        output="ERROR contacting the model pool: timed out",
        project_scope=str(other),
        model_failure_kind="transport",
        model_failure_transient=True,
    )

    with pytest.raises(RuntimeError) as caught:
        master_orchestrator.repository_worker_result(receipt, str(tmp_path))

    assert "scope mismatch" in str(caught.value)
    assert not isinstance(caught.value, agent_outcomes.WorkerModelFailure)


def _capacity_worker(calls):
    def worker(prompt, project):
        calls.append(prompt)
        receipt = autopilot_controller.HostTaskResult(
            output="ERROR contacting Sonder's model pool: it is busy; retry in 5s.",
            project_scope=project,
            model_failure_kind="transport",
            model_failure_transient=True,
        )
        return master_orchestrator.repository_worker_result(receipt, project)

    return worker


def test_pool_backpressure_worker_is_retried_and_master_reports_capacity(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "1")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    result = master_orchestrator.run_delegated(
        "Review the repository source files", worker_fn=_capacity_worker(calls),
        audit_fn=lambda prompt: "merged", agents=2, project=str(tmp_path),
    )

    assert len(calls) == 4, "each of the two workers must be retried once"
    assert "model capacity unavailable; retry" in result["output"]
    assert "file_roots" not in result["output"]
    assert "EVIDENCE_REQUIRED" not in result["output"]


def test_retry_backs_off_with_jitter_instead_of_immediate_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "3")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))

    master_orchestrator.run_delegated(
        "Review the repository source files", worker_fn=_capacity_worker([]),
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(waits) == 3
    for wait, base in zip(waits, (2, 4, 8), strict=True):
        assert 0.75 * base <= wait <= 1.25 * base


def _refusal_worker(calls, *, pool_refusal, admission_remaining):
    def worker(prompt, project):
        calls.append(prompt)
        receipt = autopilot_controller.HostTaskResult(
            output="ERROR contacting Sonder's model pool: it is busy.",
            project_scope=project,
            model_failure_kind="transport",
            model_failure_transient=True,
            model_failure_pool_refusal=pool_refusal,
            model_failure_admission_remaining=admission_remaining,
        )
        return master_orchestrator.repository_worker_result(receipt, project)

    return worker


def test_backpressure_inside_the_admission_window_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "1")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    result = master_orchestrator.run_delegated(
        "Review the repository source files",
        worker_fn=_refusal_worker(calls, pool_refusal="backpressure", admission_remaining=25.0),
        audit_fn=lambda prompt: "merged", agents=2, project=str(tmp_path),
    )

    assert len(calls) == 2 and waits == []
    assert "model capacity unavailable; retry" in result["output"]
    assert "file_roots" not in result["output"]
    assert "EVIDENCE_REQUIRED" not in result["output"]


def test_backpressure_with_a_spent_window_is_retried_with_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "1")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    master_orchestrator.run_delegated(
        "Review the repository source files",
        worker_fn=_refusal_worker(calls, pool_refusal="backpressure", admission_remaining=0.0),
        audit_fn=lambda prompt: "merged", agents=2, project=str(tmp_path),
    )

    assert len(calls) == 4
    assert len(waits) == 2 and all(1.5 <= wait <= 2.5 for wait in waits)


def test_backpressure_with_an_unknown_window_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "1")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    master_orchestrator.run_delegated(
        "Review the repository source files",
        worker_fn=_refusal_worker(calls, pool_refusal="backpressure", admission_remaining=None),
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(calls) == 1 and waits == []


def test_plain_transport_failure_keeps_the_jittered_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "3")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    master_orchestrator.run_delegated(
        "Review the repository source files",
        worker_fn=_refusal_worker(calls, pool_refusal="", admission_remaining=None),
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(calls) == 4 and len(waits) == 3
    for wait, base in zip(waits, (2, 4, 8), strict=True):
        assert 0.75 * base <= wait <= 1.25 * base


def test_spent_progress_deadline_means_no_retry_and_no_zero_second_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "2")
    monkeypatch.setattr(master_orchestrator.fleet_store, "progress_deadline_seconds", lambda: 0.0)
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    master_orchestrator.run_delegated(
        "Review the repository source files", worker_fn=_capacity_worker(calls),
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(calls) == 1 and waits == []


def test_retry_wait_reads_typed_fields_not_message_text():
    wait = agent_outcomes.retry_wait_seconds
    queue_text = RuntimeError("Ollama worker queue is full")
    assert wait(queue_text, 1, 60.0) is not None, "message text alone is not a refusal"
    assert wait(ollama_pool.WorkerPoolBackpressure("x"), 1, 60.0) is None
    assert wait(agent_outcomes.WorkerModelFailure("transport", True, "x"), 1, 0.0) is None


def test_cancel_during_backoff_ends_the_lane_cancelled_without_a_second_report(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "2")
    state = {"cancel": False}
    reports = []
    remembered = []
    monkeypatch.setattr(master_orchestrator, "cancel_requested", lambda _agent: state["cancel"])
    monkeypatch.setattr(
        agent_outcomes, "retry_sleep",
        lambda _seconds, *_a: state.__setitem__("cancel", True),
    )
    monkeypatch.setattr(
        agent_outcomes, "remember_transient_failure",
        lambda *args: remembered.append(args),
    )
    real_run_worker = master_orchestrator._run_worker

    def spy(*args, **kwargs):
        sink = kwargs.get("outcome_sink")
        kwargs["outcome_sink"] = lambda outcome: (reports.append(outcome), sink and sink(outcome))
        return real_run_worker(*args, **kwargs)

    monkeypatch.setattr(master_orchestrator, "_run_worker", spy)
    calls = []

    master_orchestrator.run_delegated(
        "Review the repository source files", worker_fn=_capacity_worker(calls),
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(calls) == 1
    assert reports.count(master_orchestrator.adaptive_concurrency.Outcome.TRANSIENT_RETRY) == 1
    assert remembered == []


def test_non_transient_failure_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "2")
    waits = []
    monkeypatch.setattr(agent_outcomes, "retry_sleep", lambda s, *_a: waits.append(s))
    calls = []

    def worker(prompt, project):
        calls.append(prompt)
        raise ValueError("bad request")

    master_orchestrator.run_delegated(
        "Review the repository source files", worker_fn=worker,
        audit_fn=lambda prompt: "merged", agents=1, project=str(tmp_path),
    )

    assert len(calls) == 1 and waits == []


# --- (d) MA-10: a router transport failure does not start Autopilot ---------


def test_router_transport_error_does_not_start_autopilot(monkeypatch):
    started = []
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ModelCallError("transport", "pool busy", transient=True)
        ),
    )
    monkeypatch.setattr(server, "autopilot_start", lambda **kw: started.append(kw) or "started")
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: started.append(kw) or "ran")
    monkeypatch.setattr(
        server.autopilot_store, "snapshot",
        lambda **_kw: {"runs": [], "counts": {}},
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert started == []
    assert isinstance(output, agent_outcomes.RouterCapacityBusy)
    assert output.status == 429
    assert output.code == "MODEL_CAPACITY_BUSY"
    assert output.retryable is True
    assert output.retry_after_seconds == 5
    assert "mode: persistent Autopilot" not in output


def test_router_busy_carries_the_error_retry_after_value(monkeypatch):
    started = []
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ModelCallError("transport", "pool busy", transient=True, retry_after_seconds=7)
        ),
    )
    monkeypatch.setattr(server, "autopilot_start", lambda **kw: started.append(kw) or "started")
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: started.append(kw) or "ran")
    monkeypatch.setattr(
        server.autopilot_store, "snapshot",
        lambda **_kw: {"runs": [], "counts": {}},
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert started == []
    assert isinstance(output, agent_outcomes.RouterCapacityBusy)
    assert output.retry_after_seconds == 7
    assert "Retry after about 7s." in output


@pytest.mark.parametrize("refusal", [
    ModelCallError("rate", "host model request rate limit reached", retry_after_seconds=3),
    ModelCallError("admission", "gpu admission refused", transient=False),
    ModelCallError("http", "slow down", retry_after_seconds=7),
], ids=["host-rate", "gpu-admission", "retry-after-hint"])
def test_router_capacity_refusals_do_not_start_autopilot(monkeypatch, refusal):
    started = []
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(refusal),
    )
    monkeypatch.setattr(server, "autopilot_start", lambda **kw: started.append(kw) or "started")
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: started.append(kw) or "ran")
    monkeypatch.setattr(
        server.autopilot_store, "snapshot",
        lambda **_kw: {"runs": [], "counts": {}},
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert started == []
    assert isinstance(output, agent_outcomes.RouterCapacityBusy)
    assert output.status == 429
    assert output.retry_after_seconds >= 1
    assert "mode: persistent Autopilot" not in output


def test_autopilot_planner_treats_a_rate_refusal_as_infrastructure():
    assert autopilot_controller._provider_unavailable(
        ModelCallError("rate", "host model request rate limit reached", retry_after_seconds=3)
    )


def test_router_schema_failure_keeps_the_autopilot_fallback(monkeypatch):
    started = []
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad JSON")),
    )
    monkeypatch.setattr(server, "autopilot_start", lambda **kw: started.append(kw) or "started")
    monkeypatch.setattr(
        server.autopilot_store, "snapshot",
        lambda **_kw: {"runs": [], "counts": {}},
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert started and "mode: persistent Autopilot" in output


# --- (e) MA-10: a planner provider failure pauses, it does not fail ---------


def _plan():
    return {
        "summary": "one task",
        "criteria": ["done"],
        "tasks": [{
            "id": "t1", "title": "inspect", "kind": "inspect",
            "instruction": "read it", "success": "read",
        }],
    }


@pytest.fixture
def infra_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(autopilot_controller, "_monotonic", lambda: now[0])
    monkeypatch.setattr(
        autopilot_controller, "_sleep", lambda s: now.__setitem__(0, now[0] + s),
    )
    return now


def test_planner_provider_failure_retries_then_pauses(infra_clock):
    run = autopilot_store.create_run("Plan under contention", max_failures=1)
    calls = []

    def plan(_run):
        calls.append(infra_clock[0])
        raise ModelCallError("transport", "read timed out", transient=True)

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), plan_fn=plan,
        work_fn=lambda *_: pytest.fail("no plan, no work"),
        review_fn=lambda *_: pytest.fail("no plan, no review"),
    )

    assert result["status"] == "paused"
    assert result["summary"] == "provider unavailable; resume to retry"
    assert result["last_error"] == "provider unavailable"
    assert result["infra_retries"] == 3
    assert calls == [0, 30, 90, 210]
    assert result["failures"] == 0


def test_planner_recovers_after_a_transient_failure(infra_clock):
    run = autopilot_store.create_run("Plan recovers", max_failures=1)
    calls = []

    def plan(_run):
        calls.append(infra_clock[0])
        if len(calls) == 1:
            raise ModelCallError("busy_timeout", "pool queue exhausted", transient=True)
        return _plan()

    autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), plan_fn=plan,
        work_fn=lambda *_: "x", review_fn=lambda *_: {"decision": "pause", "reason": "stop"},
        plan_only=True,
    )

    assert calls == [0, 30]
    assert autopilot_store.get_run(run["id"])["plan"]


def test_planner_non_provider_model_error_still_fails(infra_clock):
    run = autopilot_store.create_run("Plan config error", max_failures=1)

    def plan(_run):
        raise ModelCallError("configuration", "no planner model")

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), plan_fn=plan,
        work_fn=lambda *_: "x", review_fn=lambda *_: {},
    )
    assert result["status"] == "failed"
    assert "no planner model" in result["last_error"]
    assert infra_clock[0] == 0


# --- F06 (decision 26): a cancel during a model call discards its answer ----


def test_cancel_during_a_model_call_discards_the_partial_answer(monkeypatch):
    state = {"cancel": False}

    def generate(prompt, history=None):
        state["cancel"] = True  # the cancel lands while the call is in flight
        return '{"final":"a partial answer that must be discarded"}'

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    with pytest.raises(ModelCallError) as caught:
        server._agent_impl(
            "summarise", max_steps=1, cancel_check=lambda: state["cancel"],
        )

    assert caught.value.kind == "cancelled"
    assert "partial answer" not in str(caught.value)


def test_cancel_probe_reaches_a_model_call_that_polls_it(monkeypatch):
    """The model call itself sees the foreground cancel, not just the loop."""
    import threading
    import time

    from sonder_runtime.application import foreground_turns

    seen = {}

    def fake_make_generate(*_a, **kwargs):
        probe = kwargs.get("cancel_check")

        def generate(prompt, history=None):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if probe is not None and probe():
                    seen["aborted"] = True
                    raise ModelCallError("cancelled", "model call cancelled in flight")
                time.sleep(0.01)
            return '{"final":"the call was never told to stop"}'

        return generate

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    with foreground_turns.foreground_turn("test") as node:
        timer = threading.Timer(0.2, foreground_turns.cancel, args=(node,))
        timer.start()
        try:
            with pytest.raises(ModelCallError) as caught:
                server._agent_impl("summarise", max_steps=1)
        finally:
            timer.cancel()

    assert seen.get("aborted") is True
    assert caught.value.kind == "cancelled"


@pytest.mark.parametrize("status", [408, 502, 504])
def test_router_transient_http_status_is_capacity_not_autopilot(monkeypatch, status):
    started = []
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ModelCallError("http", "upstream hiccup", transient=True, status=status)
        ),
    )
    monkeypatch.setattr(server, "autopilot_start", lambda **kw: started.append(kw) or "started")
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: started.append(kw) or "ran")
    monkeypatch.setattr(
        server.autopilot_store, "snapshot",
        lambda **_kw: {"runs": [], "counts": {}},
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert started == []
    assert isinstance(output, agent_outcomes.RouterCapacityBusy)
