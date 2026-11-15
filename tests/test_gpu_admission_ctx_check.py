"""The requested and the effective num_ctx are compared and a mismatch is loud."""
import logging

import server
from tests.support.gpu_admission_harness import (
    MODEL, FakeMetrics, FakeOllama, chat_body, make_service, wire,
)


def _records(caplog, level, needle):
    return [r for r in caplog.records
            if r.levelno == level and needle in r.getMessage()]


def test_a_mismatch_logs_one_error_and_counts(monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    fake.ps_context_override[MODEL] = 8192  # the server runs the model smaller
    metrics = FakeMetrics()
    service = make_service(tmp_path, fake, metrics=metrics)
    pool = wire(monkeypatch, server, fake, service)
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))
    server._post("/api/chat", chat_body(8192))
    server._post("/api/chat", chat_body(8192))

    assert len(_records(caplog, logging.ERROR, "num_ctx mismatch")) == 1
    assert metrics.mismatches == 1
    assert service.snapshot()["num_ctx_mismatches"] == 1
    assert pool.gpu_admission.snapshot()["num_ctx_mismatches"] == 1


def test_a_server_that_omits_context_length_is_unverifiable_once(monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    fake.report_context_length = False
    wire(monkeypatch, server, fake, make_service(tmp_path, fake))
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))
    server._post("/api/chat", chat_body(16384))

    assert _records(caplog, logging.ERROR, "num_ctx mismatch") == []
    assert len(_records(caplog, logging.INFO, "unverifiable")) == 1


def test_a_matching_window_is_silent(monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    metrics = FakeMetrics()
    wire(monkeypatch, server, fake, make_service(tmp_path, fake, metrics=metrics))
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))

    assert _records(caplog, logging.ERROR, "num_ctx mismatch") == []
    assert metrics.mismatches == 0


def test_the_check_runs_on_the_first_load_even_inside_the_interval(monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    fake.ps_context_override[MODEL] = 4096
    service = make_service(tmp_path, fake, ctx_check_interval_seconds=3600)
    wire(monkeypatch, server, fake, service)
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))

    assert len(_records(caplog, logging.ERROR, "num_ctx mismatch")) == 1
