"""Control-plane and inference latency are tracked apart.

One long generation used to fold its whole wall time (including any GPU
admission wait) into the single latency EWMA that also ranks model-less
control-plane reads, so ``/api/tags`` and ``/api/ps`` reads, and the next
model-pinned inference, moved off a healthy primary for hours.
"""
from __future__ import annotations

from contextlib import contextmanager

from sonder_runtime.adapters.inference import gpu_admission as gpu_admission_module
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool

PRIMARY = "http://127.0.0.1:11434"
WORKER = "http://127.0.0.2:11434"
MODEL = "m:latest"


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _pool(clock, *, probe_ms=None, **kwargs):
    latencies = probe_ms or {PRIMARY: 5, WORKER: 40}

    def probe(origin):
        return {"models": [MODEL], "latency_ms": latencies[origin]}

    pool = OllamaWorkerPool(PRIMARY, (WORKER,), capability_prober=probe, clock=clock, **kwargs)
    pool.refresh_capabilities()
    return pool, latencies


def _state(pool, origin):
    return next(state for state in pool._states if state.endpoint.origin == origin)


def test_long_generation_does_not_move_model_less_reads_off_the_primary():
    clock = _Clock()
    pool, _ = _pool(clock)
    assert pool.request(lambda origin: origin, idempotent=True) == PRIMARY

    def slow(origin):
        clock.now += 60.0
        return origin

    assert pool.request(slow, model=MODEL) == PRIMARY
    picks = [pool.request(lambda origin: origin, idempotent=True) for _ in range(20)]
    assert picks.count(PRIMARY) == 20, picks
    assert pool.request(lambda origin: origin, model=MODEL) == PRIMARY


def test_probe_rtt_and_inference_time_feed_separate_averages():
    clock = _Clock()
    pool, latencies = _pool(clock)
    primary = _state(pool, PRIMARY)
    assert primary.latency_ewma_ms == 5
    assert primary.inference_ewma_ms is None

    def slow(origin):
        clock.now += 60.0
        return origin

    pool.request(slow, model=MODEL)
    assert primary.latency_ewma_ms == 5
    assert primary.inference_ewma_ms == 60_000

    latencies[PRIMARY] = 500
    pool.refresh_capabilities(force=True)
    assert primary.latency_ewma_ms > 5
    assert primary.inference_ewma_ms == 60_000

    record = next(row for row in pool.status()["workers"] if row["origin"] == PRIMARY)
    assert record["inference_ewma_ms"] == 60_000
    assert record["latency_ewma_ms"] == primary.latency_ewma_ms


def test_inference_routing_compares_inference_averages_when_all_are_measured():
    clock = _Clock()
    pool, _ = _pool(clock)

    def slow(origin):
        clock.now += 60.0
        return origin

    pool.request(slow, model=MODEL)
    _state(pool, WORKER).inference_ewma_ms = 1_000.0
    assert pool.request(lambda origin: origin, model=MODEL) == WORKER


class _QueueingAdmission:
    """GPU admission double that queues for ``wait`` seconds of pool clock."""

    accepts_queue_callback = True

    def __init__(self, clock, wait):
        self.clock = clock
        self.wait = wait
        self.queued = 0

    @contextmanager
    def admit(self, path, payload, *, origin, deadline=None, caller="interactive",
              prewarm=False, on_queue=None):
        if on_queue is not None:
            self.queued += 1
            on_queue()
        self.clock.now += self.wait
        yield gpu_admission_module.passthrough_ticket(payload)


def test_gpu_admission_wait_is_not_inference_latency():
    clock = _Clock()
    admission = _QueueingAdmission(clock, wait=30.0)
    pool = OllamaWorkerPool(
        PRIMARY, capability_prober=lambda origin: {"models": [MODEL], "latency_ms": 5},
        clock=clock, gpu_admission=admission)
    pool.refresh_capabilities()
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

    def sender(origin):
        with ollama_pool.admitted(pool, "/api/chat", body, origin):
            clock.now += 2.0
            return origin

    assert pool.request(sender, model=MODEL, payload=body) == PRIMARY
    primary = _state(pool, PRIMARY)
    assert admission.queued == 1
    assert primary.inference_ewma_ms == 2_000
    assert primary.latency_ewma_ms == 5
    assert primary.inflight == 0
