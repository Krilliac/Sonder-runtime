"""Deterministic distributed Ollama worker simulations.

No test opens a socket. Fake capability reports, a manual monotonic clock, and
events exercise the same admission/state transitions used by the live pool.
"""
from __future__ import annotations

import threading
import time
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerCapabilityUnavailable,
    WorkerPoolBackpressure,
    WorkerPoolDraining,
    WorkerPoolUnavailable,
    parse_worker_origins,
    validate_worker_origin,
)
from sonder_runtime.adapters.model_transport import ModelCallError


class ManualClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.001)
    raise AssertionError("condition did not become true")


def _origins():
    return (
        "http://127.0.0.1:11434",
        ("http://127.0.0.2:11434",),
    )


def test_capability_negotiation_admits_only_a_worker_with_the_model():
    primary, workers = _origins()
    reports = {
        primary: {
            "version": "0.11.0",
            "models": ["general:latest"],
            "latency_ms": 5,
        },
        workers[0]: {
            "version": "0.11.1",
            "models": ["coder:latest"],
            "latency_ms": 25,
        },
    }
    pool = OllamaWorkerPool(
        primary, workers, capability_prober=reports.__getitem__,
    )

    # Whole-roster discovery is an explicit refresh; requests probe at most one wave.
    pool.refresh_capabilities()
    selected = pool.request(lambda origin: origin, model="coder:latest")

    assert selected == workers[0]
    status = pool.status()
    assert status["metrics"]["capability_probes"] == 2
    assert [worker["version"] for worker in status["workers"]] == [
        "0.11.0", "0.11.1",
    ]
    with pytest.raises(WorkerCapabilityUnavailable, match="missing:latest"):
        pool.request(lambda origin: origin, model="missing:latest")


def test_latency_aware_routing_uses_measured_probe_latency():
    primary, workers = _origins()

    def probe(origin):
        return {
            "models": ["shared:latest"],
            "latency_ms": 80 if origin == primary else 8,
        }

    pool = OllamaWorkerPool(primary, workers, capability_prober=probe)
    pool.refresh_capabilities()

    assert pool.request(lambda origin: origin, model="shared:latest") == workers[0]
    fast = next(row for row in pool.status()["workers"] if row["origin"] == workers[0])
    assert fast["latency_ewma_ms"] < 10
    assert pool.status()["routing"] == "latency-aware-least-inflight"


def test_bounded_backpressure_rejects_when_capacity_and_queue_are_full():
    started = threading.Event()
    release = threading.Event()
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        max_inflight_per_worker=1,
        queue_depth=0,
        admission_timeout_seconds=0,
    )

    def hold(_origin):
        started.set()
        assert release.wait(2)
        return "done"

    thread = threading.Thread(target=lambda: pool.request(hold))
    thread.start()
    assert started.wait(2)
    try:
        with pytest.raises(WorkerPoolBackpressure, match="queue is full"):
            pool.request(lambda _origin: "should not run")
        assert pool.status()["metrics"]["backpressure_rejections"] == 1
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_partition_opens_all_circuits_and_fails_fast_until_retry_deadline():
    clock = ManualClock()
    primary, workers = _origins()
    pool = OllamaWorkerPool(
        primary,
        workers,
        failure_threshold=1,
        cooldown_seconds=10,
        clock=clock,
    )
    calls = []

    def partitioned(origin):
        calls.append(origin)
        raise URLError("network partition")

    with pytest.raises(URLError, match="network partition"):
        pool.request(partitioned, idempotent=True)
    assert len(calls) == 2
    assert {row["state"] for row in pool.status()["workers"]} == {"circuit_open"}
    assert pool.status()["metrics"]["failovers"] == 1

    with pytest.raises(WorkerPoolUnavailable, match="retry after 10.000s"):
        pool.request(partitioned, idempotent=True)
    assert len(calls) == 2


def test_half_open_worker_reconnects_after_cooldown():
    clock = ManualClock()
    primary, workers = _origins()
    pool = OllamaWorkerPool(
        primary,
        workers,
        failure_threshold=1,
        cooldown_seconds=5,
        clock=clock,
    )
    fail_primary = True

    def send(origin):
        nonlocal fail_primary
        if origin == primary and fail_primary:
            fail_primary = False
            raise URLError("link down")
        return origin

    assert pool.request(send, idempotent=True) == workers[0]
    assert pool.snapshots()[0].state == "circuit_open"
    clock.advance(5)

    assert pool.request(send, idempotent=True) == primary
    primary_status = pool.status()["workers"][0]
    assert primary_status["state"] == "unknown"
    assert primary_status["consecutive_failures"] == 0
    assert pool.status()["metrics"]["reconnects"] == 1


def test_safe_drain_rejects_new_admission_and_waits_for_inflight():
    started = threading.Event()
    release = threading.Event()
    drained = []
    pool = OllamaWorkerPool("http://127.0.0.1:11434")

    def hold(_origin):
        started.set()
        assert release.wait(2)
        return "done"

    request_thread = threading.Thread(target=lambda: pool.request(hold))
    request_thread.start()
    assert started.wait(2)
    drain_thread = threading.Thread(
        target=lambda: drained.append(pool.drain(timeout_seconds=2)),
    )
    drain_thread.start()
    _wait_until(lambda: pool.status()["admission"] == "draining")
    assert pool.status()["admission"] == "draining"
    with pytest.raises(WorkerPoolDraining, match="draining"):
        pool.request(lambda _origin: "should not run")
    release.set()
    request_thread.join(2)
    drain_thread.join(2)

    assert drained == [True]
    assert {row["state"] for row in pool.status()["workers"]} == {"drained"}
    assert pool.status()["metrics"]["drain_rejections"] == 1


def test_drain_wakes_queued_admission_and_clears_waiter_accounting():
    started = threading.Event()
    release = threading.Event()
    queued_errors = []
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        queue_depth=1,
        admission_timeout_seconds=2,
    )

    def hold(_origin):
        started.set()
        assert release.wait(2)
        return "done"

    active = threading.Thread(target=lambda: pool.request(hold))
    active.start()
    assert started.wait(2)

    def queue_request():
        try:
            pool.request(lambda _origin: "should not run")
        except Exception as error:
            queued_errors.append(error)

    queued = threading.Thread(target=queue_request)
    queued.start()
    _wait_until(lambda: pool.status()["queue"]["waiting"] == 1)
    drain = threading.Thread(target=lambda: pool.drain(timeout_seconds=2))
    drain.start()
    _wait_until(lambda: bool(queued_errors))
    release.set()
    active.join(2)
    queued.join(2)
    drain.join(2)

    assert isinstance(queued_errors[0], WorkerPoolDraining)
    assert pool.status()["queue"]["waiting"] == 0


def test_origin_inventory_is_bounded_and_rejects_non_origin_urls():
    with pytest.raises(ValueError, match="without a path"):
        validate_worker_origin(
            "https://worker.example:443/api", allow_remote=True,
        )
    raw = ",".join("https://worker-%d.example:443" % index for index in range(16))
    with pytest.raises(ValueError, match="at most 15"):
        parse_worker_origins(raw)


def test_operator_status_is_bounded_and_does_not_include_error_newlines():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434", failure_threshold=1,
    )
    with pytest.raises(URLError):
        pool.request(lambda _origin: (_ for _ in ()).throw(URLError("down\nsecret")))

    rendered = "\n".join(pool.operator_status_lines())
    assert "circuit_open" in rendered
    assert "secret" not in rendered
    assert "secret" not in repr(pool.status())


def test_protocol_model_error_is_never_replayed_as_transport_failover():
    primary, workers = _origins()
    pool = OllamaWorkerPool(primary, workers)
    calls = []

    def fail_protocol(origin):
        calls.append(origin)
        raise ModelCallError("protocol", "response exceeded safety limit")

    with pytest.raises(ModelCallError, match="safety limit"):
        pool.request(fail_protocol)
    assert calls == [primary]
    assert pool.status()["metrics"]["failovers"] == 0


def test_failed_initial_probe_is_not_admitted_and_status_is_safe():
    primary, workers = _origins()

    def probe(origin):
        if origin == primary:
            raise URLError("offline")
        return {
            "version": "0.11\nforged-status",
            "models": ["coder:latest"],
            "latency_ms": 1,
        }

    pool = OllamaWorkerPool(primary, workers, capability_prober=probe)
    calls = []
    pool.refresh_capabilities()

    assert pool.request(
        lambda origin: calls.append(origin) or origin,
        model="coder:latest",
    ) == workers[0]
    assert calls == [workers[0]]
    assert pool.snapshots()[0].state == "unreachable"
    rendered = "\n".join(pool.operator_status_lines())
    assert "version=unknown" in rendered
    assert "forged-status" not in rendered


# A model-specific request used to probe exactly one unknown/stale static
# worker (round-robin) and then fail at once with "no Ollama worker advertises
# model" whenever that one worker was not the one serving the model, although
# another unknown/stale worker did. The next request probed the next worker
# and succeeded. These tests pin the bounded concurrent wave that replaced it.
_MAIN_ONLY, _NODE_ONLY = "main-only:latest", "node-only:latest"


def _two_host_pool(clock, probed, *, failing=(), **options):
    primary, workers = _origins()
    reports = {
        primary: {"models": [_MAIN_ONLY]},
        workers[0]: {"models": [_NODE_ONLY]},
    }

    def probe(origin):
        probed.append(origin)
        if origin in failing:
            raise URLError("probe refused")
        return reports[origin]

    return OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=clock, **options,
    )


def test_first_request_reaches_a_model_only_the_second_static_worker_serves():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    pool = _two_host_pool(clock, probed)

    # Nothing is probed yet and the fair cursor starts on the primary, which
    # does not hold the model.
    assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]

    assert sorted(probed) == sorted((primary, workers[0]))
    assert pool.status()["metrics"]["capability_probes"] == 2


def test_request_survives_both_inventories_expiring_after_a_full_refresh():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    pool = _two_host_pool(clock, probed)
    pool.refresh_capabilities()
    assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]

    clock.advance(pool.capability_ttl_seconds + 1)
    del probed[:]

    assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]
    assert sorted(probed) == sorted((primary, workers[0]))


def test_request_survives_both_inventories_expiring_after_single_model_use():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    pool = _two_host_pool(clock, probed)
    # A run that only uses the primary's model. The single-worker probe left
    # the cursor on the worker here, so the request after expiry probed the
    # worker alone and refused a model the primary serves.
    assert pool.request(lambda origin: origin, model=_MAIN_ONLY) == primary
    clock.advance(pool.capability_ttl_seconds + 1)

    assert pool.request(lambda origin: origin, model=_MAIN_ONLY) == primary


def test_failing_probe_of_one_worker_does_not_fail_a_model_another_worker_serves():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    pool = _two_host_pool(clock, probed, failing=(primary,))

    assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]

    assert pool.snapshots()[0].state == "unreachable"
    assert pool.status()["metrics"]["capability_probe_failures"] == 1
    # The model the unreachable primary would serve is still refused.
    with pytest.raises(WorkerCapabilityUnavailable, match="main-only"):
        pool.request(lambda origin: pytest.fail("dispatched"), model=_MAIN_ONLY)


@pytest.mark.parametrize("limit", [
    {"capability_probe_batch_size": 2},
    {"capability_probe_parallelism": 2},
])
def test_request_time_probing_stays_within_one_concurrent_wave(limit):
    primary = "http://127.0.0.1:11434"
    workers = ("http://127.0.0.2:11434", "http://127.0.0.3:11434")
    probed = []

    def probe(origin):
        probed.append(origin)
        return {"models": ["third-only:latest"] if origin == workers[1] else []}

    pool = OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=ManualClock(), **limit,
    )

    # One request never probes more than one concurrent wave (the smaller of
    # the configured batch and parallelism), so the third worker is left for
    # the next request rather than fanned out to.
    with pytest.raises(WorkerCapabilityUnavailable, match="third-only"):
        pool.request(lambda origin: pytest.fail("dispatched"), model="third-only")
    assert sorted(probed) == sorted((primary, workers[0]))

    assert pool.request(lambda origin: origin, model="third-only") == workers[1]
    assert probed[2:] == [workers[1]]


def test_concurrent_requests_join_one_probe_batch_without_probing_twice():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    entered, release = threading.Event(), threading.Event()
    reports = {
        primary: {"models": [_MAIN_ONLY]},
        workers[0]: {"models": [_NODE_ONLY]},
    }

    def probe(origin):
        probed.append(origin)
        entered.set()
        assert release.wait(5)
        return reports[origin]

    pool = OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=clock,
        admission_timeout_seconds=10, capability_probe_timeout_seconds=10,
    )
    results = []

    def request():
        try:
            results.append(pool.request(lambda origin: origin, model=_NODE_ONLY))
        except Exception as error:  # surfaced through the assertion below
            results.append(error)

    first = threading.Thread(target=request)
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=request)
    second.start()
    _wait_until(lambda: pool.status()["queue"]["waiting"] == 1)
    release.set()
    first.join(5)
    second.join(5)

    assert results == [workers[0], workers[0]]
    assert sorted(probed) == sorted((primary, workers[0]))
    assert pool.status()["queue"]["waiting"] == 0


def test_caller_joining_a_wave_on_a_larger_roster_starts_no_second_wave():
    primary = "http://127.0.0.1:11434"
    workers = tuple("http://127.0.0.%d:11434" % index for index in range(2, 9))
    probed, results = [], []
    entered, release = threading.Event(), threading.Event()

    def probe(origin):
        probed.append(origin)
        if len(probed) == 4:
            entered.set()
        assert release.wait(5)
        return {"models": ["second-only:latest"] if origin == workers[0] else []}

    pool = OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=ManualClock(),
        admission_timeout_seconds=10, capability_probe_timeout_seconds=10,
    )

    def request():
        try:
            results.append(pool.request(lambda origin: origin, model="second-only"))
        except Exception as error:  # surfaced through the assertion below
            results.append(error)

    first = threading.Thread(target=request)
    first.start()
    assert entered.wait(2)
    # The first wave moved the fair cursor on to the other four workers.
    second = threading.Thread(target=request)
    second.start()
    _wait_until(lambda: pool.status()["queue"]["waiting"] == 1)
    release.set()
    first.join(5)
    second.join(5)

    assert results == [workers[0], workers[0]]
    assert sorted(probed) == sorted((primary, *workers[:3]))


def test_worker_with_an_unreturned_probe_does_not_take_a_wave_slot():
    primary, workers = _origins()
    clock, probed = ManualClock(), []
    release = threading.Event()

    def probe(origin):
        probed.append(origin)
        if origin == primary:
            release.wait(10)  # never answers within the probe timeout
        return {"models": [_NODE_ONLY]}

    pool = OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=clock,
        capability_probe_parallelism=1, capability_probe_timeout_seconds=0.5,
    )
    try:
        # Wave of one: the primary times out and its probe stays in flight.
        with pytest.raises(WorkerCapabilityUnavailable):
            pool.request(lambda origin: pytest.fail("dispatched"), model=_NODE_ONLY)
        assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]
        assert probed == [primary, workers[0]]

        # Both inventories expire with the cursor back on the primary, whose
        # first probe has still not returned. It must not use up the wave.
        clock.advance(pool.capability_ttl_seconds + 1)
        assert pool.request(lambda origin: origin, model=_NODE_ONLY) == workers[0]
        assert probed == [primary, workers[0], workers[0]]
    finally:
        release.set()


def test_request_queued_behind_a_partial_operator_refresh_still_probes_its_wave():
    primary = "http://127.0.0.1:11434"
    workers = tuple("http://127.0.0.%d:11434" % index for index in range(2, 9))
    probed, results = [], []
    entered, release = threading.Event(), threading.Event()

    def probe(origin):
        probed.append(origin)
        if len(probed) == 2:
            entered.set()
        assert release.wait(5)
        return {"models": ["third-only:latest"] if origin == workers[1] else []}

    pool = OllamaWorkerPool(
        primary, workers, capability_prober=probe, clock=ManualClock(),
        capability_probe_batch_size=2, admission_timeout_seconds=10,
        capability_probe_timeout_seconds=10,
    )

    def request():
        try:
            results.append(pool.request(lambda origin: origin, model="third-only"))
        except Exception as error:  # surfaced through the assertion below
            results.append(error)

    # The operator batch covers only the first two workers, neither of which
    # holds the model.
    refresh = threading.Thread(target=pool.refresh_capabilities)
    refresh.start()
    assert entered.wait(2)
    requester = threading.Thread(target=request)
    requester.start()
    _wait_until(lambda: pool.status()["queue"]["waiting"] == 1)
    release.set()
    refresh.join(5)
    requester.join(5)

    assert results == [workers[1]]
    # The operator's two workers are not probed again; the request adds one
    # wave of two beyond them.
    assert sorted(probed) == sorted((primary, *workers[:3]))
