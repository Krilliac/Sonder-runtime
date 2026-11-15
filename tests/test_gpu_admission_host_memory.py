"""A cold load is admitted against host RAM and commit headroom, not VRAM alone."""
import threading
import time

import pytest

import server
from tests.support.gpu_admission_harness import (
    DIGEST, GIB, MODEL, FakeOllama, chat_body, make_service, wire,
)

# 12 GB of RAM and 10 GB of commit available against 4 GB floors.
LOW_HOST = (12 * GIB, 10 * GIB)


def _seed_measured(service, *, ctx=16384, working_set=17 * GIB):
    if service is not None:
        service.record_footprint(DIGEST, ctx, working_set, working_set)


def test_a_measured_17gb_footprint_is_refused_before_the_transport(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="refuse")
    wire(monkeypatch, server, fake, service)
    _seed_measured(service)

    with pytest.raises(server.ModelCallError) as refused:
        server._post_model("/api/chat", chat_body(16384), model=MODEL)

    error = refused.value
    assert error.kind == "admission"
    assert error.transient is False
    assert error.attempts == 1
    assert error.refusal.reason.name == "HOST_RAM_FLOOR"
    assert error.refusal.reading.available_physical_bytes == 12 * GIB
    assert error.refusal.footprint.working_set_bytes == 17 * GIB
    assert fake.chat_bodies == []


def test_an_unmeasured_model_is_estimated_from_the_tags_size(monkeypatch, tmp_path):
    # 9.5 GB file * 125% + 1 GiB overhead is about 12.9 GB: 12 GB available fails.
    fake = FakeOllama()
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="refuse")
    wire(monkeypatch, server, fake, service)

    with pytest.raises(server.ModelCallError) as refused:
        server._post_model("/api/chat", chat_body(16384), model=MODEL)

    assert refused.value.kind == "admission"
    assert refused.value.refusal.footprint.source.name == "ESTIMATED"
    assert fake.chat_bodies == []


def test_a_model_that_fits_is_admitted(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, host=(64 * GIB, 64 * GIB), on_host_floor="refuse")
    wire(monkeypatch, server, fake, service)
    _seed_measured(service)

    server._post_model("/api/chat", chat_body(16384), model=MODEL)

    assert len(fake.chat_bodies) == 1


def test_a_loaded_model_needs_no_host_memory_check(monkeypatch, tmp_path):
    fake = FakeOllama()
    fake.loaded[MODEL] = 16384
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="refuse")
    wire(monkeypatch, server, fake, service)
    _seed_measured(service)

    server._post_model("/api/chat", chat_body(16384), model=MODEL)

    assert len(fake.chat_bodies) == 1


def test_queue_mode_remeasures_until_memory_frees(monkeypatch, tmp_path):
    fake = FakeOllama()
    state = {"available": 12 * GIB}
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="queue",
                           host_recheck_interval_ms=100)
    if service is not None:
        domain_reading = type(service._host_reader())
        service._host_reader = lambda: domain_reading(
            available_physical_bytes=state["available"], total_physical_bytes=64 * GIB,
            commit_limit_bytes=96 * GIB, commit_available_bytes=64 * GIB, commit_enforced=True)
    wire(monkeypatch, server, fake, service)
    _seed_measured(service)
    threading.Timer(0.3, lambda: state.update(available=40 * GIB)).start()

    started = time.monotonic()
    server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=10)

    assert time.monotonic() - started >= 0.25
    assert len(fake.chat_bodies) == 1


def test_queue_mode_gives_up_at_the_deadline_with_the_evidence(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="queue",
                           host_recheck_interval_ms=100)
    wire(monkeypatch, server, fake, service)
    _seed_measured(service)

    with pytest.raises(server.ModelCallError) as refused:
        server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=1)

    assert refused.value.kind == "admission"
    assert refused.value.refusal.reason.name == "HOST_RAM_FLOOR"
    assert fake.chat_bodies == []
