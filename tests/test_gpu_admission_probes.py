"""Probes (/api/show, the pool prober) pass through one bounded admission gate."""
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

