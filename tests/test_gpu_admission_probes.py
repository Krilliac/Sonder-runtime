"""Probes (/api/show, the pool prober) pass through one bounded admission gate."""
import contextlib
import threading
import time

import pytest

import server
from sonder_runtime.adapters.inference import ollama_pool
from tests.support.gpu_admission_harness import (
    FakeOllama, make_service, wait_until, wire,
)


def _pool(fake, service):
    pool = ollama_pool.OllamaWorkerPool(
        "http://127.0.0.1:11434",
        capability_prober=ollama_pool._default_capability_prober(
            allow_remote=False, open_url=fake.open_url),
    )
    pool.gpu_admission = service
    return pool


def test_concurrent_probes_never_overlap_and_the_overflow_gets_probe_busy(
        monkeypatch, tmp_path):
    fake = FakeOllama()
    fake.show_gate = threading.Event()
    service = make_service(tmp_path, fake, probe_slots=1, probe_wait_ms=300)
    wire(monkeypatch, server, fake, service)
    pool = _pool(fake, service)
    errors = []

    def show():
        try:
            server._post("/api/show", {"model": "m"}, timeout=5)
        except Exception as exc:  # collected for the assertion below
            errors.append(exc)

    first = threading.Thread(target=show)
    first.start()
    assert wait_until(lambda: fake.probe_active == 1)
    second = threading.Thread(target=show)
    refresh = threading.Thread(target=lambda: pool.refresh_capabilities(force=True))
    second.start()
    refresh.start()
    time.sleep(0.1)
    assert fake.max_probe_active == 1, "a second probe ran while the first held the gate"
    fake.show_gate.set()
    for thread in (first, second, refresh):
        thread.join(5)

    assert fake.max_probe_active == 1
    assert errors == []
    assert fake.count("/api/show") == 2

    # With the gate held longer than probe_wait_ms, the next probe is refused.
    fake.show_gate = threading.Event()
    holder = threading.Thread(target=show)
    holder.start()
    assert wait_until(lambda: fake.probe_active == 1)
    with pytest.raises(server.ModelCallError) as busy:
        server._post("/api/show", {"model": "m"}, timeout=5)
    assert busy.value.kind == "admission"
    assert busy.value.refusal.reason.name == "PROBE_BUSY"
    fake.show_gate.set()
    holder.join(5)



def test_a_probe_busy_deferral_is_not_a_worker_failure():
    calls = []
    pool = ollama_pool.OllamaWorkerPool(
        "http://127.0.0.1:11434", failure_threshold=1,
        capability_prober=lambda origin: calls.append(origin) or {"models": ["m"]},
        capability_probe_timeout_seconds=1.0,
    )
    state = pool._states[0]
    from sonder_runtime.adapters.inference import gpu_admission as adapter
    from sonder_runtime.domain import gpu_admission as domain
    refusal = domain.Refused(domain.RefusalReason.PROBE_BUSY, "probe gate busy")

    class Busy:
        def acquire_probe(self, *args, **kwargs):
            raise adapter.GpuAdmissionRefused(refusal)

    pool.gpu_admission = Busy()
    pool.refresh_capabilities(force=True)
    assert calls == []
    assert state.consecutive_failures == 0
    assert state.cooldown_until <= pool._clock()
    assert state.capabilities is None
    assert not state.capability_probe_failed
    assert not state.capability_probe_inflight


LOCAL = "http://127.0.0.1:11434"
REMOTE = "https://worker.example:11434"


def _ready(_origin):
    return {"protocol": ollama_pool._PROTOCOL, "version": "test", "models": ("m",)}


@contextlib.contextmanager
def _probe_slot_held(service, seconds):
    """Another probe (a slow /api/show) holds the local probe slot for ``seconds``."""
    held, done = threading.Event(), threading.Event()

    def hold():
        with service.admit("/api/show", {"model": "m"}, origin=LOCAL):
            held.set()
            done.wait(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    try:
        yield
    finally:
        done.set()
        thread.join(5)


def test_waiting_for_the_probe_gate_is_not_a_worker_failure(tmp_path):
    # parallelism 4 > probe_slots 1; the slot is busy for longer than the probe
    # timeout and one worker is slow. Only the slow worker's own timeout counts.
    service = make_service(tmp_path, FakeOllama(), probe_slots=1, probe_wait_ms=5000)
    healthy = ("http://127.0.0.1:11435", "http://127.0.0.1:11436", "http://127.0.0.1:11437")

    def prober(origin):
        if origin == LOCAL:
            time.sleep(0.8)
        return _ready(origin)

    pool = ollama_pool.OllamaWorkerPool(
        LOCAL, healthy, failure_threshold=1, capability_probe_parallelism=4,
        capability_probe_timeout_seconds=0.3, capability_prober=prober,
        gpu_admission=service,
    )
    with _probe_slot_held(service, 0.8):
        pool.refresh_capabilities(force=True)

    states = {state.endpoint.origin: state for state in pool._states}
    for origin in healthy:
        assert states[origin].consecutive_failures == 0, origin
        assert not states[origin].capability_probe_failed, origin
        assert states[origin].capabilities is not None, origin
    assert states[LOCAL].capability_probe_failed  # its own transport timeout
    assert wait_until(lambda: not any(s.capability_probe_inflight for s in pool._states))


def test_remote_origins_skip_the_local_probe_gate(tmp_path):
    service = make_service(tmp_path, FakeOllama(), probe_slots=1, probe_wait_ms=200)
    pool = ollama_pool.OllamaWorkerPool(
        LOCAL, (REMOTE,), allow_remote=True, failure_threshold=1,
        capability_probe_timeout_seconds=1.0, capability_prober=_ready,
        gpu_admission=service,
    )
    with _probe_slot_held(service, 3.0):
        pool.refresh_capabilities(force=True)
        started = time.monotonic()
        with service.admit("/api/tags", None, origin=REMOTE):
            pass
        assert time.monotonic() - started < 0.15

    states = {state.endpoint.origin: state for state in pool._states}
    assert states[REMOTE].capabilities is not None
    assert states[REMOTE].consecutive_failures == 0
    # The loopback probe found the local slot busy: deferred, not failed.
    assert states[LOCAL].capabilities is None
    assert states[LOCAL].consecutive_failures == 0
    assert not states[LOCAL].capability_probe_failed


def test_a_management_post_holds_no_probe_slot(tmp_path):
    service = make_service(tmp_path, FakeOllama(), probe_slots=1, probe_wait_ms=300)
    with _probe_slot_held(service, 3.0):
        started = time.monotonic()
        with service.admit("/api/pull", {"name": "m"}, origin=LOCAL) as ticket:
            assert ticket.lane is None
        assert time.monotonic() - started < 0.15
