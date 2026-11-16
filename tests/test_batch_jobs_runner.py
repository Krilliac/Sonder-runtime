"""The batch runner with in-memory fake ports: ordering, dependencies, breaker, cancel, resume."""
from __future__ import annotations

import pytest

from sonder_runtime.application.batch_jobs.runner import run_job
from sonder_runtime.application.ports.batch_jobs import JobRecord, RunSettings
from sonder_runtime.domain.batch_jobs import (
    ItemSpec, JobStatus, Limits, PauseReason, StepReason, StepState, StepStatus, validate_spec,
)


def _spec(models=("m:1", "m:2")):
    stages = [{"name": "r1", "model": models[0], "prompt": "one {{text}}"}]
    if len(models) > 1:
        stages.append({"name": "r2", "model": models[1], "prompt": "two {{text}} / {{stage.r1}}"})
    return validate_spec({"name": "job", "fields": ["text"], "stages": stages}, Limits(),
                         is_cloud_model=lambda name: False)


class FakeStore:
    def __init__(self, keys, spec, settings=None, states=None):
        self.keys = list(keys)
        self.job = JobRecord(
            id="job-1", name="job", status=JobStatus.QUEUED, pause_reason="", spec=spec,
            settings=settings or RunSettings(), request_owner="", request_role="",
            segment_start_ts=0.0, created_ts=0.0, updated_ts=0.0,
        )
        self.states = dict(states or {})
        self.finished = None
        self.cancel_after = None
        self.executor = None
        self.heartbeats = 0
        self.lease_ok = True

    def claim_job(self, job_id, owner_id, owner_pid, lease_seconds):
        return self.job

    def heartbeat(self, job_id, owner_id, lease_seconds):
        self.heartbeats += 1
        return self.lease_ok

    def cancel_requested(self, job_id):
        return self.cancel_after is not None and len(self.executor.calls) >= self.cancel_after

    def item_keys(self, job_id):
        return self.keys

    def step_states(self, job_id):
        return self.states

    def finish(self, job_id, owner_id, status, pause_reason):
        self.finished = (status, pause_reason)
        return True


class FakeExecutor:
    def __init__(self, store, answers=None, fail=None):
        self.store = store
        store.executor = self
        self.calls = []        # (stage, key, model, prompt)
        self.failed = []       # (stage, key, reason)
        self.answers = answers or {}
        self.failures = fail or {}  # (stage, key) -> failure_class

    def item(self, job_id, item_key):
        return ItemSpec(item_key, {"text": "text-" + item_key})

    def read_answer(self, state):
        return self.answers[(state.stage, state.item_key)], False

    def execute(self, request):
        key = (request.stage.name, request.item_key)
        self.calls.append((*key, request.stage.model, request.prompt))
        failure = self.failures.get(key)
        if failure:
            state = StepState(*key, status=StepStatus.FAILED, run_id="run-" + "-".join(key),
                              requested_model=request.stage.model, failure_class=failure)
        else:
            self.answers[key] = "ANSWER(%s,%s)" % key
            state = StepState(*key, status=StepStatus.ANSWERED, run_id="run-" + "-".join(key),
                              requested_model=request.stage.model, served_model=request.stage.model)
        return state

    def fail(self, request, reason):
        key = (request.stage.name, request.item_key)
        self.failed.append((*key, reason))
        return StepState(*key, status=StepStatus.FAILED, failure_class="configuration", reason=reason.value)


def _run(store, executor):
    return run_job("job-1", store=store, executor=executor, owner_id="w", owner_pid=1)


def test_stage_major_runs_every_stage_one_step_first_with_one_model_switch():
    store = FakeStore(["a", "b", "c"], _spec())
    executor = FakeExecutor(store)

    outcome = _run(store, executor)

    assert outcome.status is JobStatus.COMPLETED and store.finished == (JobStatus.COMPLETED, None)
    assert [(call[0], call[1]) for call in executor.calls] == [
        ("r1", "a"), ("r1", "b"), ("r1", "c"), ("r2", "a"), ("r2", "b"), ("r2", "c")]
    models = [call[2] for call in executor.calls]
    assert sum(1 for left, right in zip(models, models[1:], strict=False) if left != right) == 1
    assert outcome.steps_run == 6 and store.heartbeats == 6


def test_a_downstream_prompt_contains_the_upstream_answer():
    store = FakeStore(["a"], _spec())
    executor = FakeExecutor(store)
    _run(store, executor)
    assert executor.calls[1][3] == "two text-a / ANSWER(r1,a)"


def test_a_failed_upstream_blocks_the_downstream_step_and_it_is_never_dispatched():
    store = FakeStore(["a", "b"], _spec())
    executor = FakeExecutor(store, fail={("r1", "a"): "empty_response"})
    outcome = _run(store, executor)
    assert outcome.status is JobStatus.COMPLETED
    assert ("r2", "a") not in [(c[0], c[1]) for c in executor.calls]
    assert [(c[0], c[1]) for c in executor.calls] == [("r1", "a"), ("r1", "b"), ("r2", "b")]


@pytest.mark.parametrize("status", [StepStatus.SKIPPED, StepStatus.UNKNOWN, StepStatus.FAILED])
def test_a_prior_unsatisfied_upstream_blocks_without_retry_setting(status):
    prior = {("r1", "a"): StepState("r1", "a", status=status, run_id="old")}
    # Only a failed step is excluded from retry by default; skipped and unknown retry once.
    store = FakeStore(["a"], _spec(), states=prior)
    executor = FakeExecutor(store)
    _run(store, executor)
    if status is StepStatus.FAILED:
        assert executor.calls == []
    else:
        assert [(c[0], c[1]) for c in executor.calls] == [("r1", "a"), ("r2", "a")]


def test_resume_never_reruns_an_answered_step_and_retries_failed_only_on_request():
    answered = StepState("r1", "a", status=StepStatus.ANSWERED, run_id="r-a")
    failed = StepState("r1", "b", status=StepStatus.FAILED, run_id="r-b", failure_class="empty_response")
    unknown = StepState("r1", "c", status=StepStatus.UNKNOWN, run_id="r-c")
    states = {(s.stage, s.item_key): s for s in (answered, failed, unknown)}

    store = FakeStore(["a", "b", "c"], _spec(("m:1",)), states=states)
    executor = FakeExecutor(store)
    _run(store, executor)
    assert [(c[0], c[1]) for c in executor.calls] == [("r1", "c")]

    store = FakeStore(["a", "b", "c"], _spec(("m:1",)), settings=RunSettings(retry_failed=True), states=states)
    executor = FakeExecutor(store)
    _run(store, executor)
    assert sorted((c[0], c[1]) for c in executor.calls) == [("r1", "b"), ("r1", "c")]


def test_a_retried_step_passes_its_prior_state_to_the_executor():
    prior = StepState("r1", "a", status=StepStatus.UNKNOWN, run_id="old-run")
    store = FakeStore(["a"], _spec(("m:1",)), states={("r1", "a"): prior})
    seen = []

    class Spy(FakeExecutor):
        def execute(self, request):
            seen.append(request.state)
            return super().execute(request)

    _run(store, Spy(store))
    assert seen == [prior]


def test_consecutive_backend_failures_pause_the_job_and_stop_dispatch():
    store = FakeStore([str(i) for i in range(10)], _spec(("m:1",)), settings=RunSettings(max_consecutive_failures=5))
    executor = FakeExecutor(store, fail={("r1", str(i)): "unavailable" for i in range(10)})
    outcome = _run(store, executor)
    assert outcome.status is JobStatus.PAUSED
    assert outcome.pause_reason is PauseReason.CONSECUTIVE_FAILURES
    assert store.finished == (JobStatus.PAUSED, PauseReason.CONSECUTIVE_FAILURES)
    assert len(executor.calls) == 5


@pytest.mark.parametrize("failure", ["unavailable", "transport", "timeout"])
def test_every_backend_failure_class_counts_toward_the_breaker(failure):
    store = FakeStore(["a", "b", "c"], _spec(("m:1",)), settings=RunSettings(max_consecutive_failures=3))
    executor = FakeExecutor(store, fail={("r1", key): failure for key in "abc"})
    assert _run(store, executor).status is JobStatus.PAUSED


def test_an_answer_or_a_non_backend_failure_resets_the_breaker():
    keys = [str(i) for i in range(6)]
    fail = {("r1", "0"): "unavailable", ("r1", "1"): "unavailable", ("r1", "2"): "empty_response",
            ("r1", "3"): "unavailable", ("r1", "4"): "unavailable"}
    store = FakeStore(keys, _spec(("m:1",)), settings=RunSettings(max_consecutive_failures=3))
    outcome = _run(store, FakeExecutor(store, fail=fail))
    assert outcome.status is JobStatus.COMPLETED


def test_a_cancel_between_steps_stops_dispatch():
    store = FakeStore(["a", "b", "c"], _spec(("m:1",)))
    store.cancel_after = 2
    executor = FakeExecutor(store)
    outcome = _run(store, executor)
    assert outcome.status is JobStatus.CANCELLED and store.finished == (JobStatus.CANCELLED, None)
    assert len(executor.calls) == 2


def test_a_lost_lease_stops_without_writing_a_status():
    store = FakeStore(["a", "b"], _spec(("m:1",)))
    store.lease_ok = False
    executor = FakeExecutor(store)
    outcome = _run(store, executor)
    assert outcome.status is JobStatus.INTERRUPTED and store.finished is None
    assert len(executor.calls) == 1


def test_an_oversized_rendered_prompt_fails_the_step_without_a_model_call():
    store = FakeStore(["a"], _spec(("m:1",)), settings=RunSettings(max_rendered_prompt_chars=5))
    executor = FakeExecutor(store)
    _run(store, executor)
    assert executor.calls == [] and executor.failed == [("r1", "a", StepReason.PROMPT_TOO_LARGE)]


def test_a_truncated_upstream_answer_fails_the_downstream_step():
    store = FakeStore(["a"], _spec())

    class Truncating(FakeExecutor):
        def read_answer(self, state):
            return "cut", True

    executor = Truncating(store)
    _run(store, executor)
    assert [(c[0], c[1]) for c in executor.calls] == [("r1", "a")]
    assert executor.failed == [("r2", "a", StepReason.UPSTREAM_TRUNCATED)]


def test_an_unreadable_upstream_answer_fails_the_step_with_a_typed_reason():
    from sonder_runtime.domain.common.errors import IntegrityFailure

    store = FakeStore(["a"], _spec())

    class Unreadable(FakeExecutor):
        def read_answer(self, state):
            raise IntegrityFailure("sealed answer is unavailable")

    executor = Unreadable(store)
    _run(store, executor)
    assert executor.failed == [("r2", "a", StepReason.UPSTREAM_NOT_ANSWERED)]


def test_items_added_while_running_are_picked_up_before_the_next_stage():
    store = FakeStore(["a"], _spec())
    executor = FakeExecutor(store)
    original = executor.execute

    def execute(request):
        result = original(request)
        if len(executor.calls) == 1:
            store.keys.append("late")
        return result

    executor.execute = execute
    _run(store, executor)
    assert [(c[0], c[1]) for c in executor.calls] == [("r1", "a"), ("r1", "late"), ("r2", "a"), ("r2", "late")]


def test_a_claim_that_fails_returns_interrupted_without_dispatch():
    store = FakeStore(["a"], _spec())
    store.claim_job = lambda *args: None
    executor = FakeExecutor(store)
    assert _run(store, executor).status is JobStatus.INTERRUPTED and executor.calls == []


def test_a_non_final_step_state_stops_the_job_interrupted_instead_of_looping():
    store = FakeStore(["a"], _spec(("m:1",)))
    executor = FakeExecutor(store)

    def execute(request):
        key = (request.stage.name, request.item_key)
        executor.calls.append((*key, request.stage.model, request.prompt))
        return StepState(*key, status=StepStatus.PENDING, run_id="run-x", requested_model=request.stage.model)

    def fail(request, reason):
        key = (request.stage.name, request.item_key)
        return StepState(*key, status=StepStatus.PENDING, run_id="run-x")

    executor.execute = execute
    executor.fail = fail
    outcome = _run(store, executor)
    assert outcome.status is JobStatus.INTERRUPTED
    assert store.finished[0] is JobStatus.INTERRUPTED
    assert len(executor.calls) == 1
