"""Rejected chat requests are visible to the Observatory (request.failed).

A POST /v1/chat/completions the runtime refuses before its turn starts
(unknown model, invalid body, multimodal content, ...) used to emit nothing,
so the Observatory's error count stayed at zero.  An authenticated caller's
rejection now emits exactly one content-free ``request.failed`` with
``rejected: true`` and no ``request.started`` (so it never opens a span);
unauthenticated traffic still emits nothing.
"""
import json
from datetime import datetime, timezone

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.application.observability.runtime_telemetry import RuntimeTelemetry
from tests.test_observatory_http_routes import (
    ORIGIN,
    _application,
    _chat_turn_events,
    _open_chat,
    _request,
    _serve,
)


@pytest.fixture
def local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset())
    monkeypatch.setattr(ts, "OBSERVATORY_ORIGINS", frozenset({ORIGIN}))


def _post_chat(monkeypatch, app, payload, headers=None):
    with _serve(monkeypatch, app) as port:
        status, response_headers, body = _request(
            port, "POST", "/v1/chat/completions", body=json.dumps(payload),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
    correlation = response_headers.get("x-sonder-correlation-id", "")
    return status, correlation, body


def _exported(app):
    subscription = app.producer.subscribe()
    try:
        batch = subscription.next_batch(0.1, limit=4096)
    finally:
        subscription.close()
    return "\n".join(event.line for event in batch.events)


def _unknown_model(monkeypatch):
    real = ts._chat_model_selection_error

    def selection_error(selector):
        if selector == "no-such-model:1b":
            return 400, "unknown model 'no-such-model:1b'"
        return real(selector)

    monkeypatch.setattr(ts, "_chat_model_selection_error", selection_error)


def test_unknown_model_rejection_emits_one_request_failed(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "never called")
    _unknown_model(monkeypatch)
    status, correlation, body = _post_chat(monkeypatch, app, {
        "model": "no-such-model:1b",
        "messages": [{"role": "user", "content": "PRIVATE-PROMPT"}],
    })
    assert status == 400, body
    events = _chat_turn_events(app, correlation)
    assert [e["event_type"] for e in events] == ["request.failed"]
    event = events[0]
    assert event["run_id"] == correlation
    attributes = event["attributes"]
    assert attributes["outcome"] == "failed"
    assert attributes["rejected"] is True
    assert attributes["http_status"] == 400
    assert attributes["error_code"] == "invalid_model"
    assert attributes["requested_model"] == "no-such-model:1b"
    assert attributes["surface"] == "http.chat_completions"
    assert attributes["attempts"] == 0
    exported = _exported(app)
    assert "PRIVATE-PROMPT" not in exported
    assert "unknown model" not in exported


def test_multimodal_content_rejection_is_exported_without_content(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "never called")
    status, correlation, _ = _post_chat(monkeypatch, app, {
        "model": "sonder",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "PRIVATE-PROMPT"}]}],
    })
    assert status == 400
    events = _chat_turn_events(app, correlation)
    assert [e["event_type"] for e in events] == ["request.failed"]
    assert events[0]["attributes"]["error_code"] == "invalid_messages"
    assert events[0]["attributes"]["http_status"] == 400
    assert "PRIVATE-PROMPT" not in _exported(app)


def test_free_text_model_selector_is_not_exported(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "never called")
    status, correlation, _ = _post_chat(monkeypatch, app, {
        "model": {"secret": "PRIVATE-MODEL-TEXT"},
        "messages": [{"role": "user", "content": "x"}],
    })
    assert status == 400
    events = _chat_turn_events(app, correlation)
    assert [e["event_type"] for e in events] == ["request.failed"]
    assert events[0]["attributes"]["error_code"] == "invalid_request"
    assert events[0]["attributes"].get("requested_model") in (None, "[unsafe-label]")
    assert "PRIVATE-MODEL-TEXT" not in _exported(app)


def test_admission_rejection_ends_the_started_turn_as_failed(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "never called")
    lifecycle = ts.sonder_lifecycle.get()

    def refuse(*args, **kwargs):
        raise ts.sonder_lifecycle.AdmissionRejected(
            429, "CAPACITY_EXHAUSTED", "server is at capacity", retryable=True)

    monkeypatch.setattr(lifecycle, "acquire_request_slot", refuse)
    status, correlation, _ = _post_chat(monkeypatch, app, {
        "model": "sonder", "messages": [{"role": "user", "content": "x"}],
    })
    assert status == 429
    events = _chat_turn_events(app, correlation)
    assert [e["event_type"] for e in events] == ["request.started", "request.failed"]
    assert events[1]["attributes"]["http_status"] == 429
    assert events[1]["attributes"]["error_code"] == "capacity_exhausted"
    assert "rejected" not in events[1]["attributes"]


def test_draining_rejection_exports_its_response_code(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "never called")
    lifecycle = ts.sonder_lifecycle.get()
    monkeypatch.setattr(type(lifecycle.coordinator), "draining", property(lambda self: True))

    status, correlation, body = _post_chat(monkeypatch, app, {
        "model": "sonder", "messages": [{"role": "user", "content": "x"}],
    })

    assert status == 503
    assert json.loads(body)["error"]["code"] == "DRAINING"
    events = _chat_turn_events(app, correlation)
    assert [event["event_type"] for event in events] == ["request.failed"]
    assert events[0]["attributes"]["rejected"] is True
    assert events[0]["attributes"]["http_status"] == 503
    assert events[0]["attributes"]["error_code"] == "draining"


def test_unauthenticated_rejections_emit_nothing(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "deployment-key")
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    app = _application()
    _unknown_model(monkeypatch)
    status, _, _ = _post_chat(monkeypatch, app, {
        "model": "no-such-model:1b", "messages": [{"role": "user", "content": "x"}],
    })
    assert status == 401
    assert app.producer.stats()["emitted_events"] == 0


def test_successful_turn_emits_no_rejection(monkeypatch, local_open):
    app = _application()
    _open_chat(monkeypatch, lambda *a, **k: "answer")
    status, correlation, _ = _post_chat(monkeypatch, app, {
        "model": "sonder", "messages": [{"role": "user", "content": "x"}],
    })
    assert status == 200
    events = _chat_turn_events(app, correlation)
    assert [e["event_type"] for e in events] == ["request.started", "request.completed"]


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _telemetry(sink):
    ticks = iter([10.0, 10.25])
    return RuntimeTelemetry(
        sink, session_id="rts-test", version="1",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        monotonic=lambda: next(ticks),
    )


def test_reject_request_shape_and_bounds():
    sink = _Sink()
    telemetry = _telemetry(sink)
    telemetry.reject_request(
        request_id="corr-1", surface="http.chat_completions", requested_model="x" * 300,
        http_status=400, error_code="invalid_model", total_ms=12,
    )
    (event,) = sink.events
    assert event.event_code == "request.failed"
    assert event.correlation_id == event.run_id == "corr-1"
    assert event.level == "WARNING"
    assert event.fields == {
        "outcome": "failed", "rejected": True, "surface": "http.chat_completions",
        "kind": "chat", "http_status": 400, "error_code": "invalid_model",
        "requested_model": "x" * 96, "attempts": 0, "total_ms": 12,
    }


@pytest.mark.parametrize("request_id", ["bad id with spaces", None, ""])
def test_reject_request_replaces_an_unsafe_correlation_id(request_id):
    sink = _Sink()
    _telemetry(sink).reject_request(
        request_id=request_id, surface="http.chat_completions", requested_model=None,
        http_status=400, error_code="free text!", total_ms=-5,
    )
    (event,) = sink.events
    assert event.run_id.startswith("turn-")
    assert event.fields["error_code"] == "[unsafe-label]"
    assert event.fields["total_ms"] == 0
    assert "requested_model" not in event.fields


def test_reject_request_never_raises():
    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down")

    telemetry = RuntimeTelemetry(Broken(), session_id="rts-test", version="1")
    telemetry.reject_request(request_id="c", surface="http.chat_completions",
                             requested_model="m", http_status=400, error_code="x")
    assert telemetry.emit_failures == 1
