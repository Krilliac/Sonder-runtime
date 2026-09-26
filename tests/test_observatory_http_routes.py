"""Observatory telemetry routes over a real HTTP listener (live producer protocol v1)."""
from contextlib import contextmanager
import http.client
import json
import threading
import time
from types import SimpleNamespace

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.observability.observatory_producer import ObservatoryProducer
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.capabilities.observability import RedactingTelemetrySink
from sonder_runtime.application.context import current_operation_context
from sonder_runtime.application.observability.runtime_telemetry import RuntimeTelemetry
from sonder_runtime.application.ports.telemetry_feed import FeedBatch, FeedEvent, ResumeGap
from sonder_runtime.application.session import provider_attempts
from sonder_runtime.interfaces.http.facades import observability_stream as stream
from sonder_runtime.platform.logging import Redactor

ORIGIN = "http://127.0.0.1:4173"


def _application(*, feed=True, max_subscribers=8, gateway=None):
    producer = ObservatoryProducer(version="0.9.0", node_id="test-host",
                                   instance_hex="0123456789ab",
                                   max_subscribers=max_subscribers)
    telemetry = RuntimeTelemetry(RedactingTelemetrySink(producer, Redactor()),
                                 session_id=producer.session_id, version="0.9.0")
    return SimpleNamespace(
        telemetry=telemetry if feed else None,
        telemetry_feed=producer if feed else None,
        provider_bindings=ProviderBindings.uniform("ollama"),
        model_gateway=gateway if gateway is not None else SimpleNamespace(),
        producer=producer,
    )


@pytest.fixture
def local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset())
    monkeypatch.setattr(ts, "OBSERVATORY_ORIGINS", frozenset({ORIGIN}))


@contextmanager
def _serve(monkeypatch, application):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "_live_telemetry_application", lambda *, build=False: application)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(ts, "BOUND_PORT", httpd.server_address[1])
    try:
        yield httpd.server_address[1]
    finally:
        if getattr(application, "producer", None) is not None:
            application.producer.close_subscribers()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, payload


@contextmanager
def _open_stream(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    response = conn.getresponse()
    try:
        yield response
    finally:
        conn.close()


def _read_sse_blocks(response, count):
    """Read raw SSE blocks (text between blank lines) until ``count`` data blocks."""
    blocks, current = [], []
    while sum(1 for b in blocks if any(l.startswith("data: ") for l in b)) < count:
        line = response.fp.readline()
        if not line:
            break
        text = line.decode("utf-8").rstrip("\n")
        if text == "":
            blocks.append(current)
            current = []
        else:
            current.append(text)
    return blocks


def _emit(application, n):
    for index in range(n):
        application.telemetry.session_ended(emitted_events=index, dropped_events=0)


def test_sse_framing_follows_the_protocol(monkeypatch, local_open):
    app = _application()
    _emit(app, 3)
    with _serve(monkeypatch, app) as port:
        with _open_stream(port, "/v1/observability/events",
                          {"Accept": "text/event-stream", "Origin": ORIGIN}) as response:
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
            assert response.getheader("Access-Control-Allow-Origin") == ORIGIN
            assert response.getheader("Cache-Control") == "no-store"
            blocks = _read_sse_blocks(response, 3)
    assert blocks[0] == ["retry: 2000"]
    data_blocks = blocks[1:]
    for index, block in enumerate(data_blocks):
        assert len(block) == 2 and block[0] == "id: rt-0123456789ab-%d" % index
        assert block[1].startswith("data: ")
        envelope = json.loads(block[1][len("data: "):])
        assert envelope["event_id"] == "rt-0123456789ab-%d" % index
        assert not any(line.startswith("event:") for line in block)


def test_ndjson_is_selected_by_query_or_accept(monkeypatch, local_open):
    app = _application()
    _emit(app, 2)
    with _serve(monkeypatch, app) as port:
        for path, headers in (
            ("/v1/observability/events?format=ndjson", {}),
            ("/v1/observability/events", {"Accept": "application/x-ndjson"}),
        ):
            with _open_stream(port, path, headers) as response:
                assert response.getheader("Content-Type") == "application/x-ndjson"
                lines = [json.loads(response.fp.readline()) for _ in range(2)]
            assert [line["sequence"] for line in lines] == [0, 1]


def test_resume_since_now_and_stale_ids(monkeypatch, local_open):
    app = _application()
    _emit(app, 5)
    with _serve(monkeypatch, app) as port:
        with _open_stream(port, "/v1/observability/events?last_event_id=rt-0123456789ab-0",
                          {"Last-Event-ID": "rt-0123456789ab-2"}) as response:
            blocks = _read_sse_blocks(response, 1)
        assert blocks[1][0] == "id: rt-0123456789ab-3"  # header wins over query
        with _open_stream(port, "/v1/observability/events?last_event_id=rt-ffffffffffff-9") as response:
            blocks = _read_sse_blocks(response, 1)
        assert blocks[1][0] == "id: rt-0123456789ab-0"  # unknown instance: whole window
        with _open_stream(port, "/v1/observability/events?since=now") as response:
            threading.Timer(0.2, lambda: _emit(app, 1)).start()
            blocks = _read_sse_blocks(response, 1)
        assert blocks[1][0] == "id: rt-0123456789ab-5"


def test_invalid_stream_parameters_are_rejected(monkeypatch, local_open):
    with _serve(monkeypatch, _application()) as port:
        status, _, _ = _request(port, "GET", "/v1/observability/events?format=xml")
        assert status == 400
        status, _, _ = _request(port, "GET", "/v1/observability/events?since=yesterday")
        assert status == 400


def test_subscriber_cap_answers_429_with_retry_after(monkeypatch, local_open):
    app = _application(max_subscribers=1)
    with _serve(monkeypatch, app) as port:
        with _open_stream(port, "/v1/observability/events") as first:
            assert first.status == 200
            first.fp.readline()  # retry line: the subscription is registered
            status, headers, _ = _request(port, "GET", "/v1/observability/events")
            assert status == 429
            assert headers["retry-after"]


def test_closing_subscribers_ends_open_streams_promptly(monkeypatch, local_open):
    app = _application()
    with _serve(monkeypatch, app) as port:
        with _open_stream(port, "/v1/observability/events") as response:
            response.fp.readline()
            started = time.monotonic()
            app.producer.close_subscribers()
            while response.fp.readline():
                pass
            assert time.monotonic() - started < 5
        assert app.producer.stats()["subscribers"] == 0


def test_discovery_route_serves_the_producer_document(monkeypatch, local_open):
    app = _application()
    with _serve(monkeypatch, app) as port:
        status, headers, body = _request(port, "GET", "/.well-known/sonder-telemetry",
                                         headers={"Origin": ORIGIN})
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert headers["access-control-allow-origin"] == ORIGIN
    document = json.loads(body)
    assert document["schema"] == "sonder.telemetry.producer/1"
    assert document["producer"]["role"] == "runtime"
    assert document["auth"]["required"] is False


def test_export_disabled_turns_the_routes_into_404(monkeypatch, local_open):
    app = _application(feed=False)
    with _serve(monkeypatch, app) as port:
        for path in ("/v1/observability/events", "/.well-known/sonder-telemetry",
                     "/v1/sonder/ecosystem"):
            status, _, _ = _request(port, "GET", path)
            assert status == 404, path


def test_ecosystem_route_reports_runtime_stream_and_unknown_providers(monkeypatch, local_open):
    app = _application()
    with _serve(monkeypatch, app) as port:
        status, _, body = _request(port, "GET", "/v1/sonder/ecosystem")
    assert status == 200
    document = json.loads(body)
    base = "http://127.0.0.1:%d" % port
    assert document["schema"] == "sonder.runtime.ecosystem/1"
    assert document["runtime"]["instance_id"] == "rt-0123456789ab"
    assert document["runtime"]["base_url"] == base
    assert document["observatory"]["runtime_stream"]["sse_url"] == base + "/v1/observability/events"
    assert document["observatory"]["connect_urls"] == [base]
    assert document["providers"]["status"]["ollama"] == {"provider": "ollama", "state": "unknown"}


def test_api_key_mode_requires_the_admin_bearer(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "deployment-key")
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    app = _application()
    with _serve(monkeypatch, app) as port:
        for path in ("/.well-known/sonder-telemetry", "/v1/sonder/ecosystem",
                     "/v1/observability/events"):
            status, _, _ = _request(port, "GET", path)
            assert status == 401, path
        status, _, body = _request(port, "GET", "/.well-known/sonder-telemetry",
                                   headers={"Authorization": "Bearer deployment-key"})
        assert status == 200
        assert json.loads(body)["auth"]["required"] is True


def test_non_admin_accounts_are_forbidden(monkeypatch, local_open):
    context = {"mode": "account", "authorized": True, "api_key": False,
               "account": {"username": "reader", "role": "user"}}
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda self: context)
    with _serve(monkeypatch, _application()) as port:
        status, _, _ = _request(port, "GET", "/v1/observability/events")
    assert status == 403


def test_preflights_expose_the_stream_headers(monkeypatch, local_open):
    monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset({"http://app.local"}))
    request_headers = {"Access-Control-Request-Method": "GET",
                       "Access-Control-Request-Headers": "cache-control,last-event-id"}
    with _serve(monkeypatch, _application()) as port:
        status, headers, _ = _request(port, "OPTIONS", "/v1/observability/events",
                                      headers={"Origin": ORIGIN, **request_headers})
        assert status == 204
        assert headers["access-control-allow-origin"] == ORIGIN
        assert headers["access-control-allow-methods"] == "GET, OPTIONS"
        allowed = {h.strip().lower() for h in headers["access-control-allow-headers"].split(",")}
        assert {"accept", "authorization", "cache-control", "last-event-id"} <= allowed
        # The global allowlist gains the same stream headers.
        status, headers, _ = _request(port, "OPTIONS", "/v1/chat/completions",
                                      headers={"Origin": "http://app.local", **request_headers})
        assert status == 204
        allowed = {h.strip().lower() for h in headers["access-control-allow-headers"].split(",")}
        assert {"accept", "cache-control", "last-event-id"} <= allowed


def test_observatory_origin_is_scoped_to_the_telemetry_routes(monkeypatch, local_open):
    with _serve(monkeypatch, _application()) as port:
        status, _, _ = _request(port, "OPTIONS", "/v1/chat/completions",
                                headers={"Origin": ORIGIN})
        assert status == 403
        status, _, _ = _request(
            port, "POST", "/v1/chat/completions", body=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        assert status == 403
        status, _, _ = _request(port, "GET", "/v1/observability/trace",
                                headers={"Origin": ORIGIN})
        assert status == 403
        status, _, _ = _request(port, "GET", "/v1/sonder/ecosystem",
                                headers={"Origin": "http://evil.example"})
        assert status == 403


def _open_chat(monkeypatch, fake_answer):
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *a, **k: None)
    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *a, **k: False)


def test_http_chat_turn_is_exported_with_the_correlation_id(monkeypatch, local_open):
    app = _application()
    provider_attempts.install_provider_attempt_observer(app.telemetry)
    seen = {}

    def fake_answer(prompt, history, **kwargs):
        seen["ambient"] = current_operation_context()
        provider_attempts.dispatch_provider(
            "openai-compatible", "/v1/chat/completions",
            {"model": "fake", "messages": [{"role": "user", "content": prompt}]},
            lambda: {"model": "fake", "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
        )
        return "the answer"

    _open_chat(monkeypatch, fake_answer)
    body = json.dumps({"model": "sonder",
                       "messages": [{"role": "user", "content": "PRIVATE-PROMPT"}]})
    try:
        with _serve(monkeypatch, app) as port:
            status, headers, payload = _request(
                port, "POST", "/v1/chat/completions", body=body,
                headers={"Content-Type": "application/json"},
            )
    finally:
        provider_attempts.clear_provider_attempt_observer(app.telemetry)
    assert status == 200, payload
    correlation = headers["x-sonder-correlation-id"]
    assert seen["ambient"].correlation_id == correlation
    batch = app.producer.subscribe().next_batch(0.1, limit=100)
    envelopes = [json.loads(event.line) for event in batch.events]
    turn = [e for e in envelopes if e["request_id"] == correlation]
    assert [e["event_type"] for e in turn] == [
        "request.started", "route.selected", "request.completed",
    ]
    assert all(e["run_id"] == correlation for e in turn)
    assert turn[1]["attributes"]["provider"] == "openai_compatible"
    assert turn[2]["attributes"]["http_status"] == 200
    assert turn[2]["attributes"]["prompt_tokens"] == 4
    exported = "\n".join(event.line for event in batch.events)
    assert "PRIVATE-PROMPT" not in exported and "the answer" not in exported


def test_early_rejections_never_start_a_turn(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "deployment-key")
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    app = _application()
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "x"}]})
    with _serve(monkeypatch, app) as port:
        status, _, _ = _request(port, "POST", "/v1/chat/completions", body=body,
                                headers={"Content-Type": "application/json"})
    assert status == 401
    assert app.producer.stats()["emitted_events"] == 0


def test_degraded_steps_are_named_in_the_receipt(monkeypatch, local_open):
    from sonder_runtime.application.chat import provider_bridge

    def fake_answer(prompt, history, **kwargs):
        provider_bridge.record_degradation("memory_recall_embeddings")
        return "answer"

    _open_chat(monkeypatch, fake_answer)
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "x"}]})
    with _serve(monkeypatch, _application()) as port:
        status, _, payload = _request(port, "POST", "/v1/chat/completions", body=body,
                                      headers={"Content-Type": "application/json"})
    assert status == 200
    assert json.loads(payload)["sonder_receipt"]["degraded"] == ["memory_recall_embeddings"]


class _ScriptedSubscription:
    def __init__(self, batches, gap=None):
        self._batches = list(batches)
        self.resume_gap = gap

    def next_batch(self, timeout, *, limit=256):
        return self._batches.pop(0) if self._batches else FeedBatch(closed=True)

    def close(self):
        pass


def test_frames_announce_gaps_losses_and_heartbeats():
    event = FeedEvent("rt-0123456789ab-7", 7, '{"sequence":7}')
    subscription = _ScriptedSubscription(
        [FeedBatch(), FeedBatch(events=(event,), lost=3)], gap=ResumeGap(2, 4),
    )
    ticks = iter([0.0, 20.0, 20.0, 21.0])
    frames = list(stream.stream_frames(subscription, "sse", should_stop=lambda: False,
                                       clock=lambda: next(ticks)))
    assert frames == [
        b"retry: 2000\n\n",
        b": resume-gap 2-4\n\n",
        b": keepalive\n\n",
        b": dropped 3\n\nid: rt-0123456789ab-7\ndata: {\"sequence\":7}\n\n",
    ]


def test_ndjson_frames_are_lines_with_blank_heartbeats():
    event = FeedEvent("rt-0123456789ab-1", 1, '{"sequence":1}')
    subscription = _ScriptedSubscription([FeedBatch(), FeedBatch(events=(event,))])
    ticks = iter([0.0, 16.0, 16.0, 17.0])
    frames = list(stream.stream_frames(subscription, "ndjson", should_stop=lambda: False,
                                       clock=lambda: next(ticks)))
    assert frames == [b"\n", b'{"sequence":1}\n']


def test_should_stop_ends_the_stream():
    subscription = _ScriptedSubscription([FeedBatch()] * 100)
    frames = list(stream.stream_frames(subscription, "sse", should_stop=lambda: True))
    assert frames == [b"retry: 2000\n\n"]
