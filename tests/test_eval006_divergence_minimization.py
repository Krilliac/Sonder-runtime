"""EVAL-006: earliest meaningful divergence and minimized reproducible failures.

All evaluators here are deterministic in-process fakes; no model is invoked.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from sonder_runtime.adapters.evaluation_corpus import BoundedEvaluationCorpusScanner
from sonder_runtime.adapters.evaluation_failure_corpus import JsonMinimizedFailureStore
from sonder_runtime.application.evaluation.divergence import (
    DivergenceError,
    DivergencePolicy,
    InMemoryMinimizedFailureStore,
    STRATEGY_DIFFERENTIAL,
    STRATEGY_PREFIX,
    MinimizedFailure,
    earliest_divergence,
    minimize_failure,
    replay_divergence,
    reproduce,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import ProposalLifecycle
from sonder_runtime.application.evaluation.service import EvaluationApplicationService
from sonder_runtime.application.evaluation.trajectory_replay import TrajectoryRecord, TrajectoryStep


class _KeyValueSession:
    """Stateful fake session: ``truncate`` models a candidate regression."""

    def __init__(self, *, truncate: bool) -> None:
        self._truncate = truncate
        self._compress = False
        self._store: dict[str, str] = {}

    def __call__(self, request):
        op = request["op"]
        if op == "put":
            self._store[request["k"]] = request["v"]
            return {"ok": True}
        if op == "mode":
            self._compress = bool(request["compress"])
            return {"ok": True}
        value = self._store.get(request["k"])
        if self._truncate and self._compress and isinstance(value, str) and len(value) > 3:
            value = value[:3]
        return {"value": value}


_SESSION = (
    {"op": "put", "k": "a", "v": "hello"},
    {"op": "put", "k": "b", "v": "hi"},
    {"op": "get", "k": "b"},
    {"op": "mode", "compress": True},
    {"op": "put", "k": "c", "v": "x"},
    {"op": "get", "k": "b"},
    {"op": "get", "k": "a"},
    {"op": "get", "k": "c"},
    {"op": "put", "k": "d", "v": "long-value"},
    {"op": "get", "k": "d"},
)


def _recorded_session() -> TrajectoryRecord:
    reference = _KeyValueSession(truncate=False)
    steps = tuple(TrajectoryStep(index, request, reference(request)) for index, request in enumerate(_SESSION))
    return TrajectoryRecord.from_steps("kv-session", steps, metadata={"route": "baseline"})


def _candidate():
    return _KeyValueSession(truncate=True)


def _fixed_candidate():
    return _KeyValueSession(truncate=False)


def _noisy_trajectory() -> TrajectoryRecord:
    steps = tuple(
        TrajectoryStep(index, {"x": index}, {"y": index * 2, "latency_ms": 10 + index})
        for index in range(10)
    )
    return TrajectoryRecord.from_steps("noisy", steps)


def _noisy_candidate():
    def evaluate(request):
        x = request["x"]
        return {"y": -1 if x == 7 else x * 2, "latency_ms": 999}
    return evaluate


def test_policy_filters_incidental_noise_to_find_the_decision_divergence() -> None:
    expected = _noisy_trajectory()
    raw = replay_divergence(expected, _noisy_candidate)
    assert raw is not None and raw.index == 0 and raw.changed_paths == ("latency_ms",)

    policy = DivergencePolicy(ignored_paths=("latency_ms",))
    meaningful = replay_divergence(expected, _noisy_candidate, policy)
    assert meaningful is not None
    assert (meaningful.index, meaningful.field, meaningful.changed_paths) == (7, "output", ("y",))

    decisions_only = DivergencePolicy(decision_paths=("y",))
    assert replay_divergence(expected, _noisy_candidate, decisions_only).index == 7


def test_matching_decisions_report_no_divergence_and_length_changes_do() -> None:
    expected = _noisy_trajectory()
    policy = DivergencePolicy(ignored_paths=("latency_ms",))
    assert replay_divergence(expected, lambda: (lambda request: {"y": request["x"] * 2}), policy) is None

    shorter = TrajectoryRecord.from_steps("noisy", expected.steps[:4])
    divergence = earliest_divergence(expected, shorter, policy)
    assert divergence is not None and (divergence.index, divergence.field) == (4, "step_count")


def test_stateful_session_minimizes_to_a_one_minimal_reproducing_replay() -> None:
    expected = _recorded_session()
    first = replay_divergence(expected, _candidate)
    assert first is not None and first.index == 6

    failure = minimize_failure(expected, _candidate, baseline_factory=_fixed_candidate)
    assert failure.strategy == STRATEGY_DIFFERENTIAL
    assert failure.source_indexes == (0, 3, 6)
    assert [step.input["op"] for step in failure.steps] == ["put", "mode", "get"]
    assert failure.one_minimal
    assert (failure.divergence.index, failure.divergence.field, failure.divergence.changed_paths) == (2, "output", ("value",))
    assert failure.source_digest == expected.digest
    for position in range(len(failure.steps)):
        # Independent differential check: without any one retained step, a
        # fresh baseline and a fresh candidate agree on every decision.
        subset = failure.steps[:position] + failure.steps[position + 1:]
        baseline, candidate = _fixed_candidate(), _candidate()
        assert all(baseline(step.input) == candidate(step.input) for step in subset), (
            "every retained step must be necessary"
        )

    assert reproduce(failure, _candidate) == failure.divergence
    assert reproduce(failure, _fixed_candidate) is None


def test_prefix_strategy_without_a_baseline_never_trusts_out_of_context_outputs() -> None:
    expected = _recorded_session()
    failure = minimize_failure(expected, _candidate)
    assert failure.strategy == STRATEGY_PREFIX and not failure.one_minimal
    assert failure.source_indexes == tuple(range(7))
    assert failure.divergence.index == 6
    assert reproduce(failure, _candidate) == failure.divergence
    assert reproduce(failure, _fixed_candidate) is None


def test_differential_minimization_refuses_an_unfaithful_baseline() -> None:
    with pytest.raises(DivergenceError, match="baseline does not reproduce"):
        minimize_failure(_recorded_session(), _candidate, baseline_factory=_candidate)


def test_minimized_failure_round_trips_and_rejects_tampering() -> None:
    failure = minimize_failure(_recorded_session(), _candidate, baseline_factory=_fixed_candidate)
    payload = json.loads(json.dumps(failure.as_dict()))
    restored = MinimizedFailure.from_dict(payload)
    assert restored == failure and restored.digest == failure.digest

    tampered = json.loads(json.dumps(payload))
    tampered["source_indexes"] = [0, 3, 5]
    with pytest.raises(DivergenceError):
        MinimizedFailure.from_dict(tampered)
    tampered = json.loads(json.dumps(payload))
    tampered["trajectory"]["steps"][2]["output"] = {"value": "hel"}
    with pytest.raises(ValueError):
        MinimizedFailure.from_dict(tampered)


def test_budget_exhaustion_is_reported_and_nondeterminism_is_refused() -> None:
    expected = _recorded_session()
    partial = minimize_failure(expected, _candidate, baseline_factory=_fixed_candidate, max_evaluations=4)
    assert not partial.one_minimal and partial.strategy == STRATEGY_DIFFERENTIAL
    assert reproduce(partial, _candidate) is not None

    calls = {"count": 0}

    def flaky_factory():
        calls["count"] += 1
        return _KeyValueSession(truncate=calls["count"] % 2 == 1)

    with pytest.raises(DivergenceError, match="deterministically|does not diverge"):
        minimize_failure(expected, flaky_factory, baseline_factory=_fixed_candidate)
    with pytest.raises(DivergenceError, match="does not diverge"):
        minimize_failure(expected, _fixed_candidate)


def test_policy_validation_fails_closed() -> None:
    with pytest.raises(DivergenceError):
        DivergencePolicy(fields=("input",))
    with pytest.raises(DivergenceError):
        DivergencePolicy(ignored_paths=("a..b",))
    with pytest.raises(DivergenceError):
        DivergencePolicy(decision_paths=("y", "y"))
    policy = DivergencePolicy(ignored_paths=("meta.ts",))
    assert DivergencePolicy.from_dict(policy.as_dict()) == policy


def test_state_is_not_offered_as_a_decision_field() -> None:
    # Replay copies recorded step state into the candidate, so a state
    # "divergence" could never be observed; offering it would be a silent no-op.
    with pytest.raises(DivergenceError):
        DivergencePolicy(fields=("state",))
    with pytest.raises(DivergenceError):
        DivergencePolicy(fields=("output", "state"))


def test_json_store_retains_reloads_and_refuses_tampered_records(tmp_path) -> None:
    store = JsonMinimizedFailureStore(tmp_path / "failures", max_failures=1)
    failure = minimize_failure(_recorded_session(), _candidate, baseline_factory=_fixed_candidate)
    digest = store.retain(failure)
    assert store.retain(failure) == digest
    assert store.digests() == (digest,)
    reopened = JsonMinimizedFailureStore(tmp_path / "failures")
    assert reopened.load(digest) == failure

    other = minimize_failure(_noisy_trajectory(), _noisy_candidate, DivergencePolicy(ignored_paths=("latency_ms",)))
    with pytest.raises(DivergenceError, match="full"):
        store.retain(other)

    path = tmp_path / "failures" / f"{digest}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evaluations"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DivergenceError, match="digest"):
        reopened.load(digest)
    with pytest.raises(DivergenceError):
        reopened.load("../escape")


def test_in_memory_store_is_bounded() -> None:
    store = InMemoryMinimizedFailureStore(max_failures=1)
    store.retain(minimize_failure(_recorded_session(), _candidate))
    with pytest.raises(DivergenceError, match="full"):
        store.retain(minimize_failure(_noisy_trajectory(), _noisy_candidate))


def test_service_minimizes_retains_and_replays_regressions(tmp_path) -> None:
    service = EvaluationApplicationService(
        corpus=BoundedEvaluationCorpusScanner([]), lifecycle=ProposalLifecycle(),
        failures=JsonMinimizedFailureStore(tmp_path),
    )
    expected = _recorded_session()
    assert service.earliest_divergence(expected, _candidate).index == 6
    failure = service.minimize_and_retain_failure(expected, _candidate, baseline_factory=_fixed_candidate)
    assert failure.source_indexes == (0, 3, 6)
    assert service.retained_failures() == (failure.digest,)
    assert service.reproduce_retained_failure(failure.digest, _candidate) == failure.divergence
    assert service.reproduce_retained_failure(failure.digest, _fixed_candidate) is None


def _record_digest(payload) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "failure_digest"}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_load_rechecks_the_divergence_against_the_stored_steps() -> None:
    # The record digest is plain SHA-256: an editor can recompute it.  Loading
    # must still refuse a divergence that the stored steps do not support.
    failure = minimize_failure(_recorded_session(), _candidate, baseline_factory=_fixed_candidate)
    payload = json.loads(json.dumps(failure.as_dict()))
    payload["divergence"]["expected_digest"] = "0" * 64
    payload["failure_digest"] = _record_digest(payload)
    with pytest.raises(DivergenceError, match="divergence"):
        MinimizedFailure.from_dict(payload)
    payload = json.loads(json.dumps(failure.as_dict()))
    payload["divergence"]["index"] = 0
    payload["failure_digest"] = _record_digest(payload)
    with pytest.raises(DivergenceError, match="divergence"):
        MinimizedFailure.from_dict(payload)


def test_evaluation_budget_is_a_hard_ceiling_including_checks() -> None:
    expected = _recorded_session()
    for budget in (4, 5, 8, 64):
        failure = minimize_failure(expected, _candidate, baseline_factory=_fixed_candidate, max_evaluations=budget)
        assert failure.evaluations <= budget
    assert minimize_failure(expected, _candidate, max_evaluations=3).evaluations <= 3
    with pytest.raises(DivergenceError, match="max_evaluations"):
        minimize_failure(expected, _candidate, baseline_factory=_fixed_candidate, max_evaluations=3)
    with pytest.raises(DivergenceError, match="max_evaluations"):
        minimize_failure(expected, _candidate, max_evaluations=2)


class _TwoBugSession(_KeyValueSession):
    """Also answers a missing key with a sentinel: a second, unrelated bug."""

    def __call__(self, request):
        result = super().__call__(request)
        if request["op"] == "get" and result.get("value") is None:
            return {"value": "MISSING"}
        return result


def test_minimization_preserves_the_originally_reported_divergence() -> None:
    expected = _recorded_session()
    candidate = lambda: _TwoBugSession(truncate=True)  # noqa: E731
    original = replay_divergence(expected, candidate)
    assert original is not None and original.index == 6
    failure = minimize_failure(expected, candidate, baseline_factory=_fixed_candidate)
    # Dropping the put would surface the unrelated missing-key bug at the same
    # step; the minimizer must keep reproducing the truncation instead.
    assert failure.source_indexes == (0, 3, 6)
    assert failure.source_indexes[-1] == original.index
    assert (failure.divergence.expected_digest, failure.divergence.actual_digest) == (
        original.expected_digest, original.actual_digest,
    )


def test_file_store_cleans_stale_temporaries_and_serializes_writers(tmp_path) -> None:
    directory = tmp_path / "failures"
    directory.mkdir()
    stale = directory / ("a" * 64 + ".json.orphan.tmp")
    stale.write_text("partial", encoding="utf-8")
    old = time.time() - 3_600
    os.utime(stale, (old, old))
    fresh = directory / ("b" * 64 + ".json.inflight.tmp")
    fresh.write_text("partial", encoding="utf-8")
    store = JsonMinimizedFailureStore(directory, lock_timeout_seconds=0.2)
    failure = minimize_failure(_recorded_session(), _candidate, baseline_factory=_fixed_candidate)
    store.retain(failure)
    assert not stale.exists(), "stale temporary files must be cleaned"
    assert fresh.exists(), "a recent temporary may belong to a live writer"

    other = minimize_failure(_noisy_trajectory(), _noisy_candidate, DivergencePolicy(ignored_paths=("latency_ms",)))
    (directory / ".lock").write_text("held", encoding="utf-8")
    with pytest.raises(DivergenceError, match="lock"):
        store.retain(other)
    assert store.digests() == (failure.digest,)


def test_lock_release_never_deletes_another_writers_lock(tmp_path) -> None:
    store = JsonMinimizedFailureStore(tmp_path)
    lock = tmp_path / ".lock"
    with store._locked():
        # Simulate our lock being broken as stale and re-acquired by another
        # writer while we were still inside the critical section.
        lock.write_text("another-writer-token", encoding="utf-8")
    assert lock.read_text(encoding="utf-8") == "another-writer-token"


def test_stale_lock_break_never_removes_a_fresh_lock(tmp_path) -> None:
    store = JsonMinimizedFailureStore(tmp_path)
    lock = tmp_path / ".lock"
    # A waiter judged the *old* holder's lock stale, but a new holder has since
    # replaced it: the break must leave the new holder's lock in place.
    lock.write_text("fresh-holder", encoding="utf-8")
    assert store._break_stale_lock(lock, "old-holder") is False
    assert lock.read_text(encoding="utf-8") == "fresh-holder"

    lock.write_text("old-holder", encoding="utf-8")
    assert store._break_stale_lock(lock, "old-holder") is True
    assert not lock.exists()
    assert not list(tmp_path.glob(".lock.*")), "no sidecar files may be left behind"


def test_a_stale_lock_is_broken_and_the_write_proceeds(tmp_path) -> None:
    lock = tmp_path / ".lock"
    lock.write_text("crashed-writer", encoding="utf-8")
    old = time.time() - 3_600
    os.utime(lock, (old, old))
    store = JsonMinimizedFailureStore(tmp_path, lock_timeout_seconds=0.5)
    failure = minimize_failure(_recorded_session(), _candidate, baseline_factory=_fixed_candidate)
    assert store.retain(failure) == failure.digest
    assert not lock.exists()


def test_concurrent_writers_respect_the_capacity_bound(tmp_path) -> None:
    import threading

    policy = DivergencePolicy(ignored_paths=("latency_ms",))
    failures = []
    for number in range(6):
        steps = tuple(
            TrajectoryStep(index, {"x": index}, {"y": index * 2, "latency_ms": 1})
            for index in range(10)
        )
        record = TrajectoryRecord.from_steps(f"noisy-{number}", steps)
        failures.append(minimize_failure(record, _noisy_candidate, policy))
    store = JsonMinimizedFailureStore(tmp_path, max_failures=4, lock_timeout_seconds=10)
    outcomes: list[str] = []
    barrier = threading.Barrier(len(failures))

    def write(failure) -> None:
        barrier.wait()
        try:
            store.retain(failure)
            outcomes.append("ok")
        except DivergenceError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=write, args=(failure,)) for failure in failures]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 4
    assert len(store.digests()) == 4
    assert not (tmp_path / ".lock").exists()
