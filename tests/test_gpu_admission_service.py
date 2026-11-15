"""The admission service end to end: lanes, slots, refusals, store failures, wiring."""
from __future__ import annotations

import logging
import multiprocessing
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import model_error_formatting
from sonder_runtime.adapters.inference import gpu_admission as adapter
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.inference.gpu_admission_store import GpuAdmissionStore
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain import gpu_admission as domain
from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig
from tests.support.gpu_admission_harness import (
    GIB, MODEL, FakeMetrics, FakeOllama, chat_body, make_service, wait_until, wire,
)

ORIGIN = "http://127.0.0.1:11434"


def _chat(service, num_ctx=4096, **kwargs):
    with service.admit("/api/chat", chat_body(num_ctx), origin=ORIGIN, **kwargs) as ticket:
        return ticket


def test_a_probe_never_takes_the_gpu_slot(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1)
    wire(monkeypatch, server, fake, service)
    with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
        with service.admit("/api/show", {"model": MODEL}, origin=ORIGIN) as ticket:
            assert ticket.lane is None
    assert service.snapshot()["admitted"] == 1


def test_an_unload_is_admitted_while_a_slot_is_held_and_clears_the_pin(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1)
    holder_done = threading.Event()
    held = threading.Event()

    def hold():
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
            held.set()
            holder_done.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(5)
        assert service._the_store().get_pin(ORIGIN, adapter.model_key(MODEL)).num_ctx == 4096
        unload = {"model": MODEL, "keep_alive": 0}
        started = time.monotonic()
        with service.admit("/api/generate", unload, origin=ORIGIN):
            pass
        assert time.monotonic() - started < 1.0
        assert service._the_store().get_pin(ORIGIN, adapter.model_key(MODEL)) is None
    finally:
        holder_done.set()
        thread.join(5)


def test_the_exact_policy_never_serves_a_probe_at_another_window(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake)
    _chat(service, 8192)
    fake.loaded[MODEL] = 8192
    with service.admit("/api/chat", chat_body(4096), origin=ORIGIN,
                       ctx_policy=domain.CtxPolicy.EXACT) as ticket:
        assert ticket.served_num_ctx != 8192 or ticket.rewritten is False
        assert (ticket.payload["options"]["num_ctx"]) == 4096


def test_refusal_is_a_non_transient_admission_error_and_not_retryable():
    refusal = domain.Refused(domain.RefusalReason.HOST_RAM_FLOOR, "low", retry_after_seconds=7.0)
    error = adapter.GpuAdmissionRefused(refusal)
    assert isinstance(error, ModelCallError)
    assert (error.kind, error.transient, error.retry_after_seconds) == ("admission", False, 7.0)
    assert ollama_pool.OllamaWorkerPool._retryable(error) is False
    again = error.restamped(attempts=3, cloud=False)
    assert isinstance(again, adapter.GpuAdmissionRefused)
    assert again.attempts == 3 and again.refusal is refusal
    assert "host_ram_floor" in model_error_formatting.format_model_call_error(
        error, target="ollama", display="m")


def test_the_gateway_maps_an_admission_refusal_to_a_conflict():
    from sonder_runtime.adapters.inference import ollama_gateway
    from sonder_runtime.domain.common.errors import Conflict
    refusal = domain.Refused(domain.RefusalReason.QUEUE_FULL, "full", retry_after_seconds=5.0)
    mapped = ollama_gateway._map_model_error(adapter.GpuAdmissionRefused(refusal))
    assert isinstance(mapped, Conflict)
    assert mapped.retry_after_seconds == 5.0


def _corrupt(tmp_path):
    path = tmp_path / "gpu-admission.sqlite3"
    path.write_bytes(b"not a sqlite database" * 100)
    return path


def test_enforce_on_a_corrupt_store_refuses_with_store_unavailable(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake)
    _corrupt(tmp_path)
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        _chat(service)
    assert caught.value.refusal.reason is domain.RefusalReason.STORE_UNAVAILABLE


def test_observe_on_a_corrupt_store_admits_and_logs(tmp_path, caplog):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, mode="observe")
    _corrupt(tmp_path)
    with caplog.at_level(logging.WARNING):
        ticket = _chat(service, 4096)
    assert ticket.rewritten is False
    assert any("admitting without coordination" in r.getMessage() for r in caplog.records)


def _install_foreign_owner(monkeypatch, tmp_path):
    from sonder_runtime.adapters.persistence import owned_sqlite
    private = tmp_path / "private-root"
    private.mkdir()
    monkeypatch.setattr(owned_sqlite, "_PROCESS_OWNER",
                        owned_sqlite.OwnedSQLiteConnections((private,)))


def test_enforce_with_a_process_owner_refusing_the_store_path_is_store_unavailable(
        tmp_path, monkeypatch):
    fake = FakeOllama()
    (tmp_path / "outside").mkdir()
    service = make_service(tmp_path / "outside", fake)
    _install_foreign_owner(monkeypatch, tmp_path)
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        _chat(service)
    assert caught.value.refusal.reason is domain.RefusalReason.STORE_UNAVAILABLE


def test_observe_with_a_process_owner_refusing_the_store_path_admits_and_logs(
        tmp_path, monkeypatch, caplog):
    fake = FakeOllama()
    (tmp_path / "outside").mkdir()
    service = make_service(tmp_path / "outside", fake, mode="observe")
    _install_foreign_owner(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING):
        ticket = _chat(service, 4096)
    assert ticket.rewritten is False
    assert any("admitting without coordination" in r.getMessage() for r in caplog.records)


def test_a_wrong_schema_store_is_unavailable_in_enforce(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake)
    path = tmp_path / "gpu-admission.sqlite3"
    service._the_store()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE gpu_meta SET value='999' WHERE key='schema_version'")
    service._store = GpuAdmissionStore(path)
    service._verified = False
    with pytest.raises(adapter.GpuAdmissionRefused) as caught:
        _chat(service)
    assert caught.value.refusal.reason is domain.RefusalReason.STORE_UNAVAILABLE


def test_off_mode_touches_no_store(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, mode="off")
    ticket = _chat(service)
    assert ticket.lane is None and ticket.rewritten is False
    assert not (tmp_path / "gpu-admission.sqlite3").exists()
    assert service.snapshot()["admitted"] == 0


def test_cloud_models_and_remote_origins_skip_the_slot(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1)
    with service.admit("/api/chat", chat_body(4096, model="gpt-oss:120b-cloud"), origin=ORIGIN):
        with service.admit("/api/chat", chat_body(4096), origin="http://10.1.2.3:11434") as remote:
            assert remote.lane is domain.AdmissionLane.INFERENCE
    assert service._the_store().leases() == ()


def test_a_nested_admit_in_the_same_context_passes_through(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1)
    with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN) as inner:
            assert inner.lane is None


def test_interactive_waiters_are_served_before_background_waiters(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1)
    order = []
    release = threading.Event()
    held = threading.Event()

    def hold():
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
            held.set()
            release.wait(5)

    def waiter(name, caller):
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN, caller=caller):
            order.append(name)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    background = threading.Thread(target=waiter, args=("bg", "background"))
    background.start()
    assert wait_until(lambda: service.snapshot()["queued"] >= 1)
    interactive = threading.Thread(target=waiter, args=("ui", "interactive"))
    interactive.start()
    assert wait_until(lambda: service.snapshot()["queued"] >= 2)
    release.set()
    for thread in (holder, background, interactive):
        thread.join(10)
    assert order == ["ui", "bg"]


def test_the_queue_times_out_with_queue_timeout(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1, max_queue_wait_seconds=1)
    release = threading.Event()
    held = threading.Event()

    def hold():
        with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        with pytest.raises(adapter.GpuAdmissionRefused) as caught:
            _chat(service)
        assert caught.value.refusal.reason is domain.RefusalReason.QUEUE_TIMEOUT
    finally:
        release.set()
        holder.join(5)


def test_the_get_path_with_the_pool_disabled_is_admitted(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, probe_slots=1)
    wire(monkeypatch, server, fake, service)
    seen = []
    real = service.admit

    def spy(path, payload, **kwargs):
        seen.append(path)
        return real(path, payload, **kwargs)

    monkeypatch.setattr(service, "admit", spy)
    server._get("/api/version")
    assert "/api/version" in seen


def test_pool_summary_carries_admission_only_when_enabled(tmp_path):
    fake = FakeOllama()
    on = make_service(tmp_path, fake)
    off = make_service(tmp_path, fake, mode="off")
    for service, present in ((on, True), (off, False)):
        pool = ollama_pool.OllamaWorkerPool(ORIGIN)
        pool.gpu_admission = service
        assert ("gpu_admission" in pool.summary()) is present


def test_prewarm_is_a_load_and_a_cold_load_stores_its_footprint(tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake)
    service._memory_reader = lambda _key: (6 * GIB, 7 * GIB)
    body = {"model": MODEL, "options": {"num_ctx": 4096}, "keep_alive": "5m"}
    with service.admit("/api/generate", body, origin=ORIGIN, prewarm=True) as ticket:
        assert ticket.lane is domain.AdmissionLane.LOAD
    digest = next(iter(service._read_tags(ORIGIN).values())).digest
    rows = service._the_store().footprints_for(digest)
    assert [(r.num_ctx, r.working_set_bytes, r.private_bytes) for r in rows] == [
        (4096, 6 * GIB, 7 * GIB)]


def test_a_killed_service_holder_is_reclaimed(tmp_path):
    script = (
        "import sys, time\n"
        "from sonder_runtime.adapters.inference.gpu_admission_store import GpuAdmissionStore\n"
        "s = GpuAdmissionStore(sys.argv[1])\n"
        "print(s.acquire('doomed', 'gpu', slots=1, priority=0, origin=%r, model_key='x',"
        " ttl_seconds=600, max_depth=8), flush=True)\n"
        "time.sleep(600)\n" % ORIGIN
    )
    import subprocess
    fake = FakeOllama()
    service = make_service(tmp_path, fake, gpu_slots=1, max_queue_wait_seconds=30)
    path = tmp_path / "gpu-admission.sqlite3"
    service._the_store()
    child = subprocess.Popen([sys.executable, "-c", script, str(path)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        threading.Timer(0.5, child.kill).start()
        ticket = _chat(service)
        assert ticket.lane is domain.AdmissionLane.INFERENCE
    finally:
        if child.poll() is None:
            child.kill()
        child.stdout.close()


def _service_worker(directory, name, results):
    from tests.support.gpu_admission_harness import FakeOllama as _Fake, make_service as _make
    from pathlib import Path
    service = _make(Path(directory), _Fake(), gpu_slots=1, max_queue_wait_seconds=60)
    with service.admit("/api/chat", chat_body(4096), origin=ORIGIN):
        started = time.time()
        time.sleep(0.4)
        ended = time.time()
    results.put((name, started, ended))


def test_two_service_processes_never_overlap_with_one_slot(tmp_path):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [context.Process(target=_service_worker, args=(str(tmp_path), n, results))
               for n in ("a", "b")]
    for worker in workers:
        worker.start()
    intervals = sorted(results.get(timeout=90)[1:] for _ in workers)
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    (_s1, end_first), (start_second, _e2) = intervals
    assert start_second >= end_first - 0.02, intervals


def test_the_typed_binding_reaches_the_pool_built_from_the_environment():
    try:
        adapter.configure_typed_gpu_admission(GpuAdmissionConfig(mode="observe", gpu_slots=3))
        pool = ollama_pool.from_environment(ORIGIN)
        assert pool.gpu_admission.config.gpu_slots == 3
        explicit = ollama_pool.from_environment(
            ORIGIN, environment={"SONDER_GPU_SLOTS": "2"})
        assert explicit.gpu_admission.mode == "off"
        assert explicit.gpu_admission.config.gpu_slots == 2
    finally:
        adapter.reset_typed_gpu_admission()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path default")
def test_the_default_store_is_machine_wide_not_the_state_home():
    path = adapter.default_store_path({"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"})
    assert str(path).endswith("Sonder\\gpu-admission.sqlite3")


def test_a_mismatch_counts_in_the_snapshot_and_the_metric(tmp_path, caplog):
    fake = FakeOllama()
    metrics = FakeMetrics()
    service = make_service(tmp_path, fake, metrics=metrics)
    calls = []

    def ps(_origin):
        calls.append(1)
        if len(calls) == 1:
            return {"models": []}
        return {"models": [{"name": MODEL, "size": GIB, "size_vram": GIB, "context_length": 2048}]}

    service._ps_reader = ps
    with caplog.at_level(logging.ERROR):
        _chat(service, 4096)
    assert metrics.mismatches == 1
    assert service.snapshot()["num_ctx_mismatches"] == 1
    assert SimpleNamespace  # keep import used
