"""Bounded authenticated batch completions through a real loopback HTTP peer."""
from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway
from sonder_runtime.adapters.inference.openrouter_gateway import (
    OpenRouterCreditsExhausted,
    OpenRouterGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission, ModelRequestRateConfig
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    Cancelled, CapacityExceeded, DeadlineExceeded, Forbidden, InvalidInput,
)
from tests.test_openrouter_gateway import FAKE_KEY, MODEL, _chat


class BatchPeer:
    def __init__(self):
        self.requests = []
        self.finished = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.status = {}
        self.before = lambda _prompt: None
        self.after = lambda _prompt: None

    def handle(self, handler):
        body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        with self.lock:
            self.requests.append((handler.path, dict(handler.headers), body))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.before(prompt)
            status = self.status.get(prompt, 200)
            document = (_chat(prompt, model=body["model"]) if status == 200 else
                        {"error": {"code": status, "message": "synthetic batch failure"}})
            data = json.dumps(document).encode()
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            try:
                handler.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the deadline test intentionally closes the client first
            with self.lock:
                self.finished.append(prompt)
            self.after(prompt)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def peer():
    state = BatchPeer()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state.handle(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = "http://127.0.0.1:%d/api/v1" % server.server_address[1]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def gateway(peer, **env):
    return OpenRouterGateway(env={
        "SONDER_ALLOW_CLOUD": "1", "OPENROUTER_API_KEY": FAKE_KEY,
        "SONDER_OPENROUTER_BASE_URL": peer.base_url,
        "SONDER_OPENROUTER_MODEL": MODEL, **env,
    }, policy_models=None)


def context(**kwargs):
    return local_owner_context(correlation_id="batch-test", cloud_allowed=True, **kwargs)


def requests(*prompts):
    return tuple(ModelRequest(prompt, "code") for prompt in prompts)


def test_batch_orders_results_not_completion_and_accounts_each_authenticated_send(peer, monkeypatch):
    second_done = threading.Event()
    peer.before = lambda prompt: second_done.wait(3) if prompt == "first" else None
    peer.after = lambda prompt: second_done.set() if prompt == "second" else None
    usage = []
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append,
    )
    results = gateway(peer).generate_batch(requests("first", "second"), context(), max_workers=2)
    assert peer.finished == ["second", "first"]
    assert [item.response.text for item in results] == ["first", "second"]
    assert all(item.error is None for item in results)
    assert all((item.response.tokens_in, item.response.tokens_out) == (12, 5) for item in results)
    assert all(item.response.telemetry.prompt_cached_tokens == 4 for item in results)
    assert len(usage) == len(peer.requests) == 2
    assert sum(item["cost_usd"] for item in usage) == pytest.approx(0.00084)
    for path, headers, body in peer.requests:
        assert path == "/api/v1/chat/completions"
        assert headers["Authorization"] == "Bearer " + FAKE_KEY
        assert body["stream"] is False
        assert body["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}


def test_batch_physical_concurrency_never_exceeds_requested_workers(peer):
    barrier = threading.Barrier(2)
    peer.before = lambda _prompt: barrier.wait(timeout=3)
    results = gateway(peer).generate_batch(requests(*map(str, range(6))), context(), max_workers=2)
    assert peer.peak == 2
    assert len(peer.requests) == 6
    assert [item.response.text for item in results] == list(map(str, range(6)))


def test_batch_partial_failure_preserves_siblings_without_retries(peer):
    peer.status["bad"] = 402
    results = gateway(peer).generate_batch(requests("good", "bad", "also-good"), context())
    assert results[0].response.text == "good"
    assert results[1].response is None
    assert isinstance(results[1].error, OpenRouterCreditsExhausted)
    assert results[2].response.text == "also-good"
    assert sorted(body["messages"][-1]["content"] for _, _, body in peer.requests) == [
        "also-good", "bad", "good",
    ]


def test_batch_keeps_each_request_model_tier_and_options(peer):
    instance = gateway(peer, SONDER_OPENROUTER_TIER_MODELS="reasoning=deepseek/deepseek-r1")
    batch = [ModelRequest("explicit", "code", options={"model": "openai/gpt-5-mini", "num_predict": 16}),
             ModelRequest("tier", "reasoning", options={"temperature": 0.1})]
    results = instance.generate_batch(batch, context())
    assert [item.response.model for item in results] == ["openai/gpt-5-mini", "deepseek/deepseek-r1"]
    assert [item.response.tier for item in results] == ["code", "reasoning"]
    payloads = {body["messages"][-1]["content"]: body for _, _, body in peer.requests}
    assert payloads["explicit"]["max_tokens"] == 16
    assert payloads["tier"]["temperature"] == 0.1


def test_batch_physical_rate_admission_applies_to_each_request(peer, tmp_path):
    instance = gateway(peer)
    instance._request_admission = HostModelRequestAdmission(
        ModelRequestRateConfig(burst=1, requests_per_minute=1),
        db_path=tmp_path / "admission.sqlite3",
    )
    results = instance.generate_batch(requests("first", "denied"), context(), max_workers=1)
    assert results[0].response.text == "first"
    assert isinstance(results[1].error, CapacityExceeded)
    assert len(peer.requests) == 1


@pytest.mark.parametrize("max_workers", [0, 9, True, 1.5, "2"])
def test_invalid_worker_limit_sends_nothing(peer, max_workers):
    with pytest.raises(InvalidInput):
        gateway(peer).generate_batch(requests("valid"), context(), max_workers=max_workers)
    assert peer.requests == []


@pytest.mark.parametrize("bad", [
    [ModelRequest("", "code")], [object()], [ModelRequest("x", "code", stream=True)],
    [ModelRequest("x", "code", options={"model": "no-slash"})],
    [ModelRequest("x", "code", options={"num_predict": "invalid"})],
    [ModelRequest("x", "code", options={"format": {"type": "object", "properties": set()}})],
    [ModelRequest("x", "code", options={"format": {"value": float("nan")}})],
    [ModelRequest("x", "code")] * 65, "text", iter([ModelRequest("x", "code")]),
])
def test_invalid_entire_batch_sends_nothing_even_with_valid_first_request(peer, bad):
    batch = [ModelRequest("valid", "code"), *bad] if isinstance(bad, list) else bad
    with pytest.raises(InvalidInput):
        gateway(peer).generate_batch(batch, context())
    assert peer.requests == []


def test_empty_batch_is_empty_and_sends_nothing(peer):
    assert gateway(peer).generate_batch((), context()) == ()
    assert peer.requests == []


@pytest.mark.parametrize("env,cloud_context,error", [
    ({"SONDER_ALLOW_CLOUD": "0"}, True, Forbidden), ({}, False, Forbidden),
    ({"OPENROUTER_API_KEY": ""}, True, InvalidInput),
])
def test_batch_preserves_cloud_and_key_controls(peer, env, cloud_context, error):
    ctx = local_owner_context(correlation_id="cloud-test", cloud_allowed=cloud_context)
    results = gateway(peer, **env).generate_batch(requests("one", "two"), ctx)
    assert all(item.response is None and isinstance(item.error, error) for item in results)
    assert peer.requests == []


class Cancellation:
    def __init__(self):
        self.event = threading.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


def test_pending_cancellation_preserves_completed_success_and_never_dispatches_tail(peer):
    cancel = Cancellation()
    peer.before = lambda prompt: cancel.event.set() if prompt == "cancel" else None
    results = gateway(peer).generate_batch(
        requests("success", "cancel", "pending"), context(cancellation=cancel), max_workers=1,
    )
    assert results[0].response.text == "success"
    assert isinstance(results[1].error, Cancelled)
    assert isinstance(results[2].error, Cancelled)
    assert [body["messages"][-1]["content"] for _, _, body in peer.requests] == ["success", "cancel"]


def test_pending_deadline_preserves_completed_success_and_never_dispatches_tail(peer):
    peer.before = lambda prompt: time.sleep(0.5) if prompt == "slow" else None
    results = gateway(peer).generate_batch(
        requests("success", "slow", "pending"), context(timeout_seconds=0.3), max_workers=1,
    )
    assert results[0].response.text == "success"
    assert isinstance(results[1].error, DeadlineExceeded)
    assert isinstance(results[2].error, DeadlineExceeded)
    assert [body["messages"][-1]["content"] for _, _, body in peer.requests] == ["success", "slow"]


@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_completed_response_accounts_usage_before_control_stop_and_refuses_tail(peer, monkeypatch, control):
    from sonder_runtime.application import context as context_module

    clock = [0.0]
    monkeypatch.setattr(context_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    cancel = Cancellation()
    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    instance = gateway(peer)

    def transport(url, payload, headers, timeout):
        data = OpenAICompatibleGateway._default_transport(url, payload, headers, timeout)
        if control == "cancel":
            cancel.event.set()
        else:
            clock[0] = 11.0
        return data

    instance._raw_post = transport
    results = instance.generate_batch(
        requests("completed", "pending"), context(cancellation=cancel, timeout_seconds=10), max_workers=1,
    )
    expected = Cancelled if control == "cancel" else DeadlineExceeded
    assert all(isinstance(item.error, expected) for item in results)
    assert len(peer.requests) == len(usage) == 1
    assert usage[0]["cost_usd"] == pytest.approx(0.00042)
    assert usage[0]["prompt_tokens"] == 12
    assert usage[0]["completion_tokens"] == 5
    assert instance.last_usage == usage[0]


@pytest.mark.parametrize("cancelled", [True, False])
def test_preexisting_control_stop_sends_nothing(peer, cancelled):
    cancel = Cancellation()
    if cancelled:
        cancel.event.set()
    ctx = context(cancellation=cancel, timeout_seconds=None if cancelled else 0)
    results = gateway(peer).generate_batch(requests("one", "two"), ctx)
    expected = Cancelled if cancelled else DeadlineExceeded
    assert all(isinstance(item.error, expected) for item in results)
    assert peer.requests == []


def test_batch_copies_ambient_context_independently_into_every_transport(peer):
    marker = ContextVar("batch-test-marker", default="absent")
    observed = []

    def transport(url, payload, headers, timeout):
        observed.append(marker.get())
        marker.set("worker-only")
        return OpenAICompatibleGateway._default_transport(url, payload, headers, timeout)

    instance = gateway(peer)
    instance._raw_post = transport
    token = marker.set("parent")
    try:
        results = instance.generate_batch(requests("one", "two", "three"), context(), max_workers=1)
        assert all(item.response is not None for item in results)
        assert observed == ["parent"] * 3
        assert marker.get() == "parent"
    finally:
        marker.reset(token)


def test_batch_accepts_the_documented_request_and_worker_ceiling(peer):
    results = gateway(peer).generate_batch(requests(*map(str, range(64))), context(), max_workers=8)
    assert len(results) == len(peer.requests) == 64
    assert all(item.response is not None for item in results)
