"""Exactly one request owns a worker's half-open probe, and its end ends it.

A half-open flag that nobody clears strands the worker: ``_choose`` skips it
forever and every request for its models fails as unavailable.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference import gpu_admission as gpu_admission_module
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerPoolBackpressure,
    WorkerPoolDraining,
    WorkerPoolUnavailable,
    pool_refusal_reply,
)
from sonder_runtime.interfaces.http.facades.observatory_routes import is_legacy_error_reply

PRIMARY = "http://127.0.0.1:11434"
LOCAL = "qwen3:9b-q8_0"


class _GatedAdmission:
    """GPU admission double: queues every request until ``go`` is set."""

    mode = "enforce"
    accepts_queue_callback = True

    def __init__(self):
        self.go = threading.Event()
        self.parked = threading.Event()

    def snapshot(self):
        return {}

    @contextmanager
    def admit(self, path, payload, *, origin, deadline=None, caller="interactive",
              prewarm=False, on_queue=None):
        if on_queue is not None:
            on_queue()
        self.parked.set()
        assert self.go.wait(10)
        yield gpu_admission_module.passthrough_ticket(payload)


def _pool(*, gpu=None, cap=2):
    now = [1000.0]
    pool = OllamaWorkerPool(
        PRIMARY, capability_prober=lambda _origin: {"models": [LOCAL]},
        gpu_admission=gpu, max_inflight_per_worker=cap, failure_threshold=1,
        cooldown_seconds=1, clock=lambda: now[0])
    pool.refresh_capabilities()
    return pool, pool._states[0], now


def _lease(pool):
    box = []
    pool._acquire(model=LOCAL, excluded=set(), admission_timeout=0,
                  _lease_box=box, _lease_caller="local")
    return box[0]


def _reaches_worker(pool, now):
    now[0] += 100.0  # past any cooldown
    return pool.request(lambda _origin: "ok", model=LOCAL, admission_timeout_seconds=0.2)


def test_unparked_request_that_becomes_the_probe_and_fails_ends_the_probe():
    gpu = _GatedAdmission()
    pool, primary, now = _pool(gpu=gpu)
    seen = {}

    def sender(origin):
        with ollama_pool.admitted(pool, "/api/chat", {"model": LOCAL}, origin,
                                  deadline=time.monotonic() + 5):
            seen["owner"] = primary.half_open_owner == ollama_pool._ACTIVE_LEASE.get()[1].id
            raise URLError("connection reset")

    outcome = {}

    def run():
        try:
            pool.request(sender, model=LOCAL, caller="local")
        except BaseException as error:  # the test inspects the outcome
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert gpu.parked.wait(5)
    with pool._condition:
        primary.consecutive_failures = 1  # tripped while parked; no cooldown left
    gpu.go.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), URLError)
    assert seen["owner"] is True
    assert primary.half_open_inflight is False
    assert primary.half_open_owner is None
    assert _reaches_worker(pool, now) == "ok"


def test_reclaiming_a_dead_probe_owner_ends_the_probe_despite_a_live_sibling():
    pool, primary, now = _pool()
    sibling = _lease(pool)
    with pool._condition:
        primary.consecutive_failures = 1
    owned = []
    owner_thread = threading.Thread(target=lambda: owned.append(_lease(pool)), daemon=True)
    owner_thread.start()
    owner_thread.join(5)
    assert primary.half_open_inflight is True
    assert primary.half_open_owner == owned[0].id

    pool.summary()  # reclaims the lease of the exited thread
    assert primary.half_open_inflight is False
    assert primary.half_open_owner is None
    pool._close_lease(sibling, error=ValueError("model error"), count_failure=False)
    assert _reaches_worker(pool, now) == "ok"


def test_a_stale_probe_owner_cannot_end_a_newer_probe():
    pool, primary, now = _pool(cap=4)
    first = _lease(pool)
    with pool._condition:
        primary.consecutive_failures = 1
    stale = _lease(pool)  # owns the probe
    assert primary.half_open_owner == stale.id
    pool._close_lease(first, error=None, latency_ms=1)  # the worker answered: probe over
    assert primary.half_open_inflight is False and primary.half_open_owner is None
    tripping = _lease(pool)
    pool._close_lease(tripping, error=URLError("reset"), latency_ms=1)
    now[0] += 100.0
    newer = _lease(pool)  # the next probe
    assert primary.half_open_owner == newer.id

    pool._close_lease(stale, error=ValueError("late model error"), count_failure=False)
    assert primary.half_open_inflight is True
    assert primary.half_open_owner == newer.id
    with pytest.raises(WorkerPoolUnavailable):
        _lease(pool)  # no second concurrent probe
    pool._close_lease(newer, error=None, latency_ms=1)
    assert primary.half_open_inflight is False
    assert _reaches_worker(pool, now) == "ok"


def test_unpark_after_the_deadline_refuses_without_charging_the_worker():
    gpu = _GatedAdmission()
    pool, primary, _now = _pool(gpu=gpu)
    deadline = time.monotonic() + 0.2
    sent = []

    def sender(origin):
        with ollama_pool.admitted(pool, "/api/chat", {"model": LOCAL}, origin, deadline=deadline):
            if deadline - time.monotonic() <= 0:
                raise TimeoutError("request deadline expired")
            sent.append(origin)
            return "ok"

    outcome = {}

    def run():
        try:
            outcome["value"] = pool.request(sender, model=LOCAL, caller="local")
        except BaseException as error:  # the test inspects the outcome
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert gpu.parked.wait(5)
    time.sleep(max(0.0, deadline - time.monotonic()) + 0.05)
    gpu.go.set()
    thread.join(5)
    assert isinstance(outcome.get("error"), WorkerPoolBackpressure), outcome
    assert sent == []
    assert primary.consecutive_failures == 0
    assert pool._metrics["transport_failures"] == 0
    assert primary.inflight == 0


def test_pool_refusals_get_a_truthful_reply():
    busy = pool_refusal_reply(WorkerPoolBackpressure("Ollama worker queue is full"))
    assert busy.startswith("ERROR contacting Sonder's model pool: it is busy")
    assert "retry in 5s" in busy
    assert "Is the Ollama server running" not in busy
    assert is_legacy_error_reply(busy)

    down = pool_refusal_reply(WorkerPoolUnavailable(
        "all Ollama workers are unavailable; retry after 12.200s", retry_after_seconds=12.2))
    assert "no model worker is available" in down and "retry in 13s" in down
    assert is_legacy_error_reply(down)

    draining = pool_refusal_reply(WorkerPoolDraining("Ollama worker pool is draining"))
    assert "shutting down" in draining and is_legacy_error_reply(draining)

    assert pool_refusal_reply(URLError("connection refused")) is None
    assert pool_refusal_reply(ValueError("x")) is None


def test_admission_refusals_carry_their_retry_hint():
    pool, primary, now = _pool(cap=1)
    with pool._condition:
        primary.cooldown_until = now[0] + 7.5
    with pytest.raises(WorkerPoolUnavailable) as caught:
        pool.request(lambda _origin: "ok", model=LOCAL, admission_timeout_seconds=0)
    assert caught.value.retry_after_seconds == pytest.approx(7.5)
    assert "retry in 8s" in pool_refusal_reply(caught.value)


def test_reclaiming_dead_non_owner_keeps_live_siblings_probe():
    pool, primary, now = _pool(cap=4)
    dead_leases = []
    dead_thread = threading.Thread(target=lambda: dead_leases.append(_lease(pool)), daemon=True)
    dead_thread.start()
    dead_thread.join(5)
    assert len(dead_leases) == 1
    with pool._condition:
        primary.consecutive_failures = 1
    owner = _lease(pool)  # live thread owns half_open_owner
    assert primary.half_open_owner == owner.id
    assert dead_leases[0].id != owner.id

    pool.summary()  # reclaims only the dead non-owner lease
    assert primary.half_open_inflight is True
    assert primary.half_open_owner == owner.id
    pool._close_lease(owner, error=None, latency_ms=1)
    assert primary.half_open_inflight is False
    assert _reaches_worker(pool, now) == "ok"


def test_unpark_allows_lease_that_already_owns_half_open_probe():
    pool, primary, _now = _pool(cap=2)
    lease = _lease(pool)
    with pool._condition:
        primary.consecutive_failures = 1
        primary.half_open_inflight = True
        primary.half_open_owner = lease.id
        primary.inflight -= 1
        lease.counted = False

    pool._unpark_lease(lease, deadline=time.monotonic() + 5)
    assert lease.counted is True
    assert primary.half_open_inflight is True
    assert primary.half_open_owner == lease.id
    pool._close_lease(lease, error=None, latency_ms=1)
