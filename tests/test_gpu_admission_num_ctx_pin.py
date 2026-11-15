"""One num_ctx per loaded model: a second window is served pinned, not reloaded."""
import logging

import pytest

import server
from tests.support.gpu_admission_harness import (
    MODEL, FakeOllama, chat_body, make_service, wire,
)


def _pinned_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "pinned" in r.getMessage()]


def test_smaller_request_is_served_at_the_pinned_window_with_one_warning(
        monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    wire(monkeypatch, server, fake, make_service(tmp_path, fake))
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))
    server._post("/api/chat", chat_body(2048))
    server._post("/api/chat", chat_body(2048))

    assert [b["options"]["num_ctx"] for b in fake.chat_bodies] == [16384, 16384, 16384]
    assert fake.reloads == 0
    assert len(_pinned_warnings(caplog)) == 1


def test_a_different_requested_window_logs_again_for_its_own_tuple(
        monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    wire(monkeypatch, server, fake, make_service(tmp_path, fake))
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(16384))
    server._post("/api/chat", chat_body(2048))
    server._post("/api/chat", chat_body(4096))

    assert [b["options"]["num_ctx"] for b in fake.chat_bodies] == [16384] * 3
    assert len(_pinned_warnings(caplog)) == 2


def test_wait_unload_raises_a_typed_conflict_instead_of_reloading(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, ctx_conflict="wait_unload",
                           max_queue_wait_seconds=1)
    wire(monkeypatch, server, fake, service)

    server._post("/api/chat", chat_body(16384))
    with pytest.raises(server.ModelCallError) as refused:
        server._post("/api/chat", chat_body(2048), timeout=1)

    assert refused.value.kind == "admission"
    assert fake.reloads == 0
    assert [b["options"]["num_ctx"] for b in fake.chat_bodies] == [16384]


def test_a_prompt_that_does_not_fit_the_pin_is_refused_not_truncated(monkeypatch, tmp_path):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, max_repins_per_model=0)
    wire(monkeypatch, server, fake, service)

    server._post("/api/chat", chat_body(2048))
    with pytest.raises(server.ModelCallError) as refused:
        server._post("/api/chat", chat_body(16384, content="word " * 4000))

    assert refused.value.kind == "admission"
    assert fake.reloads == 0


def test_a_prompt_that_does_not_fit_repins_once_when_idle(monkeypatch, tmp_path, caplog):
    fake = FakeOllama()
    wire(monkeypatch, server, fake, make_service(tmp_path, fake, max_repins_per_model=1))
    caplog.set_level(logging.DEBUG)

    server._post("/api/chat", chat_body(2048))
    server._post("/api/chat", chat_body(16384, content="word " * 4000))

    assert [b["options"]["num_ctx"] for b in fake.chat_bodies] == [2048, 16384]
    assert fake.reloads == 1
    assert any("repin" in r.getMessage() for r in caplog.records)
