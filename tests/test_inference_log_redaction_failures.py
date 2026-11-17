"""Transport failures never expose synthetic endpoint credentials in diagnostics."""
from __future__ import annotations

import contextlib
import http.client
import io
import logging
import traceback
import urllib.error

import pytest

from sonder_runtime.adapters.inference import (
    gpu_admission,
    ollama_endpoint,
    ollama_vision,
)
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)

pytestmark = pytest.mark.unit
MARKER = "syntheticcredentialmarker761"
BASE = "http://127.0.0.1:8080/v1?api_key=" + MARKER


def _assert_private_diagnostics(caplog, error=None):
    formatter = logging.Formatter()
    surfaces = {"formatted logs": "\n".join(formatter.format(record) for record in caplog.records)}
    if error is not None:
        surfaces["public error"] = str(error)
        pending, seen, parts = [error], set(), []
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            parts.extend((str(current), repr(current)))
            parts.extend(traceback.format_exception(current, chain=False))
            # Suppressed contexts still exist and can be rendered by another caller.
            pending.extend((current.__cause__, current.__context__))
        surfaces["complete exception graph"] = "\n".join(parts)
    assert not [name for name, text in surfaces.items() if MARKER in text]


class _CountingAdmission(HostModelRequestAdmission):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def try_acquire(self):
        self.calls += 1
        return super().try_acquire()


def _failure(kind, url):
    if kind.startswith("http-"):
        return urllib.error.HTTPError(
            url, int(kind.removeprefix("http-")), "request failed for " + url, {},
            io.BytesIO(b'{"error":"unavailable"}'),
        )
    if kind == "timeout":
        return TimeoutError("request " + MARKER)
    if kind == "url-timeout":
        return urllib.error.URLError(TimeoutError("request " + MARKER))
    if kind == "url-error":
        return urllib.error.URLError(OSError("request " + MARKER))
    if kind == "os-error":
        return OSError("request " + MARKER)
    raise AssertionError("unknown synthetic failure kind")


@pytest.mark.parametrize("kind, expected, retryable", [
    ("http-401", Forbidden, False),
    ("http-422", InvalidInput, False),
    ("http-429", CapacityExceeded, True),
    ("http-500", DependencyUnavailable, True),
    ("timeout", DeadlineExceeded, True),
    ("url-timeout", DeadlineExceeded, True),
    ("url-error", DependencyUnavailable, True),
    ("os-error", DependencyUnavailable, True),
])
def test_post_failure_classification_does_not_expose_credentials(caplog, kind, expected, retryable):
    caplog.set_level(logging.DEBUG)
    calls = []
    admission = _CountingAdmission()
    cfg = OpenAICompatibleConfig(base_url=BASE, model="synthetic-model")

    def transport(url, payload, headers, timeout):
        calls.append((url, timeout))
        raise _failure(kind, url)

    gateway = OpenAICompatibleGateway(cfg, transport=transport, request_admission=admission)
    with pytest.raises(expected) as caught:
        gateway._post("/embeddings", {}, cfg, 2.0)
    assert type(caught.value) is expected
    assert caught.value.retryable is retryable
    assert calls == [(BASE + "/embeddings", 2.0)]
    assert admission.calls == 1
    assert caplog.records
    _assert_private_diagnostics(caplog, caught.value)


@pytest.mark.parametrize("kind, expected", [
    ("timeout", DeadlineExceeded),
    ("url-timeout", DeadlineExceeded),
    ("url-error", DependencyUnavailable),
    ("os-error", DependencyUnavailable),
])
def test_get_failure_classification_does_not_expose_credentials(caplog, kind, expected):
    caplog.set_level(logging.DEBUG)
    calls = []
    admission = _CountingAdmission()
    cfg = OpenAICompatibleConfig(base_url=BASE, model="synthetic-model")

    def transport(url, headers, timeout):
        calls.append((url, timeout))
        raise _failure(kind, url)

    gateway = OpenAICompatibleGateway(cfg, get_transport=transport, request_admission=admission)
    with pytest.raises(expected) as caught:
        gateway.get_json("/models", timeout=2.0, cfg=cfg)
    assert type(caught.value) is expected
    assert caught.value.retryable is True
    assert calls == [(BASE + "/models", 2.0)]
    assert admission.calls == 0
    _assert_private_diagnostics(caplog, caught.value)


@pytest.mark.parametrize("method", ["post", "get"])
def test_connect_classifier_result_is_preserved_without_raw_exception_chain(caplog, method):
    caplog.set_level(logging.DEBUG)
    calls, classified = [], []
    cfg = OpenAICompatibleConfig(base_url=BASE, model="synthetic-model")
    reason = OSError("request " + MARKER)

    def transport(*args):
        calls.append(args[0])
        raise urllib.error.URLError(reason)

    def classify(error):
        classified.append(error)
        return Forbidden("synthetic provider policy refused the connection")

    gateway = OpenAICompatibleGateway(
        cfg, transport=transport, get_transport=transport,
        request_admission=_CountingAdmission(), connect_error_classifier=classify,
    )
    with pytest.raises(Forbidden) as caught:
        if method == "post":
            gateway._post("/embeddings", {}, cfg, 2.0)
        else:
            gateway.get_json("/models", timeout=2.0, cfg=cfg)
    assert len(calls) == 1
    assert classified == [reason]
    assert caught.value.retryable is False
    _assert_private_diagnostics(caplog, caught.value)


def test_http_classifier_receives_status_and_bounded_body_without_leaking_its_cause(caplog):
    caplog.set_level(logging.DEBUG)
    classified, calls = [], []
    cfg = OpenAICompatibleConfig(base_url=BASE, model="synthetic-model")

    def transport(url, payload, headers, timeout):
        calls.append(url)
        raise _failure("http-503", url)

    def classify(status, body):
        classified.append((status, body))
        return CapacityExceeded("synthetic provider capacity refusal")

    gateway = OpenAICompatibleGateway(
        cfg, transport=transport, request_admission=_CountingAdmission(),
        http_error_classifier=classify,
    )
    with pytest.raises(CapacityExceeded) as caught:
        gateway._post("/embeddings", {}, cfg, 2.0)
    assert calls == [BASE + "/embeddings"]
    assert classified == [(503, b'{"error":"unavailable"}')]
    assert caught.value.retryable is True
    _assert_private_diagnostics(caplog, caught.value)


def test_get_non_success_http_status_is_still_returned_to_the_caller(caplog):
    caplog.set_level(logging.DEBUG)
    cfg = OpenAICompatibleConfig(base_url=BASE, model="synthetic-model")
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return 503, b'{"error":"unavailable"}'

    gateway = OpenAICompatibleGateway(cfg, get_transport=transport, request_admission=_CountingAdmission())
    assert gateway.get_json("/models", timeout=2.0, cfg=cfg) == (503, {"error": "unavailable"})
    assert calls == [BASE + "/models"]
    _assert_private_diagnostics(caplog)


def test_vision_malformed_response_has_no_credential_in_any_exception_chain(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    admissions, exited, sends = [], [], []
    payload = {"model": "synthetic-model", "messages": []}
    url = "http://127.0.0.1:11434/api/chat?api_key=" + MARKER

    class Admission:
        @contextlib.contextmanager
        def admit(self, path, data, *, origin, **kwargs):
            admissions.append((path, origin))
            assert data is payload
            try:
                yield gpu_admission.passthrough_ticket(data)
            finally:
                exited.append(True)

    def open_url(request, timeout):
        sends.append((request.full_url, timeout))
        raise http.client.BadStatusLine("request " + MARKER)

    monkeypatch.setattr(ollama_vision.gpu_admission, "process_service", lambda: Admission())
    monkeypatch.setattr(ollama_endpoint, "open_url", open_url)
    with pytest.raises(DependencyUnavailable) as caught:
        ollama_vision.OllamaVisionGateway._post(url, payload, 2.0)
    assert caught.value.retryable is True
    assert admissions == [("/api/chat", "http://127.0.0.1:11434")]
    assert exited == [True]
    assert sends == [(url, 2.0)]
    assert any("vision request failed" in record.getMessage() for record in caplog.records)
    _assert_private_diagnostics(caplog, caught.value)


def test_endpoint_configuration_info_drops_ambiguous_authority_credentials(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ollama_endpoint, "_configured_endpoint", None)
    value = "http://" + MARKER + "/unescaped@127.0.0.1:11434"
    try:
        ollama_endpoint.configure_typed_endpoint(value)
    finally:
        ollama_endpoint.reset_typed_endpoint()
    assert any("Ollama endpoint configured:" in record.getMessage() for record in caplog.records)
    _assert_private_diagnostics(caplog)
