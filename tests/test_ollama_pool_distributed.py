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
        pool.request(partitioned)
    assert len(calls) == 2
    assert {row["state"] for row in pool.status()["workers"]} == {"circuit_open"}
    assert pool.status()["metrics"]["failovers"] == 1

    with pytest.raises(WorkerPoolUnavailable, match="retry after 10.000s"):
        pool.request(partitioned)
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

    assert pool.request(send) == workers[0]
    assert pool.snapshots()[0].state == "circuit_open"
    clock.advance(5)

    assert pool.request(send) == primary
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
    with pytest.raises(ValueError, match="without path"):
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

    assert pool.request(
        lambda origin: calls.append(origin) or origin,
        model="coder:latest",
    ) == workers[0]
    assert calls == [workers[0]]
    assert pool.snapshots()[0].state == "unreachable"
    rendered = "\n".join(pool.operator_status_lines())
    assert "0.11 forged-status" in rendered
    assert "\nforged-status" not in rendered
