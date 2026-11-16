"""Inference adapters must not put URL credentials in raw log records."""
from __future__ import annotations

import io
import json
import logging
import socket
import urllib.error
import urllib.request

import pytest

from sonder_runtime.adapters.inference import gpu_admission, ollama_endpoint, ollama_vision
from sonder_runtime.adapters.inference.log_safe import redact_url
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.application.ports.vision_gateway import VisionRequest
from sonder_runtime.domain.common.errors import DependencyUnavailable, Forbidden, SonderError
from sonder_runtime.domain.gpu_admission import HostMemoryReading
from sonder_runtime.platform.gpu_admission_config import GpuAdmissionConfig

SECRET = "s3cret"
CRED_ORIGIN = "http://user:s3cret@127.0.0.1:11434"
CRED_URL = "http://user:s3cret@127.0.0.1:11434/v1?api_key=s3cret#s3cret"


def _assert_clean(caplog) -> None:
    assert caplog.records, "expected log records to be captured"
    formatter = logging.Formatter()
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args)
        if record.exc_info:
            assert SECRET not in formatter.formatException(record.exc_info)


def _assert_emitted(caplog, fragment: str) -> None:
    assert any(fragment in r.getMessage() for r in caplog.records), (
        "expected a log record containing %r" % fragment
    )


def test_redact_url_strips_userinfo_query_fragment():
    assert redact_url(CRED_URL) == "http://127.0.0.1:11434/v1"
    assert redact_url("HTTPS://[::1]:8080/a/b?x=1") == "https://[::1]:8080/a/b"


@pytest.mark.parametrize(
    "value",
    [
        "http://user:p/s3cret@127.0.0.1:11434/v1",
        "http://user:p?s3cret@127.0.0.1:11434",
        "http://user:p#s3cret@127.0.0.1:11434",
        "http://s3cret@127.0.0.1:11434",
        "http://user:a@b:s3cret@127.0.0.1:11434",
    ],
)
def test_redact_url_drops_userinfo_with_unescaped_delimiters(value):
    rendered = redact_url(value)
    assert SECRET not in rendered and "user" not in rendered
    assert rendered.startswith("http://127.0.0.1:11434") or rendered == "http://<redacted>"


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8080/v1?user=me@example.com&api_key=SECRETKEY",
        "http://127.0.0.1:8080/v1#frag@SECRETFRAG",
        "http://h/p@s3cret",
    ],
)
def test_redact_url_at_sign_after_delimiter_is_not_userinfo(value):
    rendered = redact_url(value)
    assert rendered == "http://<redacted>"
    for leaked in ("SECRETKEY", "SECRETFRAG", "s3cret", "example.com"):
        assert leaked not in rendered


def test_redact_url_userinfo_before_delimiters_versus_after():
    assert redact_url("HTTP://u:p@127.0.0.1:1/a?b=c@d") == "http://<redacted>"
    assert redact_url("http://u:p@127.0.0.1:1/a") == "http://127.0.0.1:1/a"


@pytest.mark.parametrize("value", ["", None, 42, "not a url", "http://", "http://[bad"])
def test_redact_url_never_raises(value):
    assert redact_url(value) == "<invalid url>"


def test_configure_typed_endpoint_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    try:
        ollama_endpoint.configure_typed_endpoint(CRED_URL)
    finally:
        ollama_endpoint.reset_typed_endpoint()
    _assert_emitted(caplog, "configure_typed_endpoint: value=")
    _assert_clean(caplog)


def test_configured_origin_failure_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        ollama_endpoint.configured_origin(CRED_ORIGIN)
    assert any("policy error" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_configured_origin_success_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ollama_endpoint, "normalize", lambda value=None: CRED_ORIGIN)
    monkeypatch.setattr(ollama_endpoint, "policy_error", lambda *a, **k: "")
    assert ollama_endpoint.configured_origin(CRED_ORIGIN) == CRED_ORIGIN
    assert any("resolved to" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_open_url_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError):
        ollama_endpoint.open_url(urllib.request.Request(CRED_URL))
    assert any("open_url" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_vision_analyze_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ollama_vision.ollama_endpoint, "normalize", lambda: CRED_URL)
    monkeypatch.setattr(ollama_vision.ollama_endpoint, "is_loopback", lambda value: True)
    gateway = ollama_vision.OllamaVisionGateway(
        target_resolver=lambda tier, strict: ModelTarget("llava-local", False, "vision"),
        transport=lambda url, payload, timeout: {"message": {"content": "ok"}},
    )
    gateway.analyze(
        VisionRequest("describe", b"pixels", "image/png"),
        local_owner_context(correlation_id="redact"),
    )
    assert any("posting to" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def test_vision_post_failure_does_not_log_credentials(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    def boom():
        raise RuntimeError("admission unavailable")

    monkeypatch.setattr(ollama_vision.gpu_admission, "process_service", boom)
    with pytest.raises(DependencyUnavailable):
        ollama_vision.OllamaVisionGateway._post(CRED_URL, {}, 1.0)
    _assert_emitted(caplog, "Ollama vision request failed")
    _assert_emitted(caplog, "Ollama vision inference request failed")
    _assert_clean(caplog)


def _gateway(transport) -> tuple[OpenAICompatibleGateway, OpenAICompatibleConfig]:
    cfg = OpenAICompatibleConfig(
        base_url="http://user:s3cret@127.0.0.1:8080/v1?api_key=s3cret",
        model="m",
    )
    return OpenAICompatibleGateway(cfg, transport=transport), cfg


def test_openai_compat_post_success_does_not_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    gateway, cfg = _gateway(lambda url, payload, headers, timeout: {"ok": True})
    assert gateway._post("/embeddings", {}, cfg, 5.0) == {"ok": True}
    assert any("_post" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


@pytest.mark.parametrize(
    "error, fragment",
    [
        (_http_error(401), "authentication failed"),
        (_http_error(429), "rate limited"),
        (_http_error(500), "returned server error"),
        (socket.timeout("t"), "timed out: url="),
        (urllib.error.URLError(socket.timeout("t")), "timed out (URLError)"),
        (urllib.error.URLError("refused"), "endpoint unreachable: url="),
        (OSError("down"), "unreachable (OSError)"),
    ],
    ids=["401", "429", "500", "timeout", "urlerror-timeout", "urlerror", "oserror"],
)
def test_openai_compat_post_failures_do_not_log_credentials(caplog, error, fragment):
    caplog.set_level(logging.DEBUG)

    def transport(url, payload, headers, timeout):
        raise error

    gateway, cfg = _gateway(transport)
    with pytest.raises(SonderError):
        gateway._post("/embeddings", {}, cfg, 5.0)
    _assert_emitted(caplog, fragment)
    _assert_clean(caplog)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:s3cret@api.example.com/v1?api_key=s3cret",
        "http://user:s3cret@:8080/v1",
    ],
)
def test_openai_compat_forbidden_messages_do_not_carry_credentials(base_url):
    gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(base_url=base_url, model="m"))
    cfg = gateway._resolved_config()
    with pytest.raises(Forbidden) as consent:
        gateway._enforce_consent(cfg, local_owner_context(correlation_id="redact"))
    assert SECRET not in str(consent.value)
    with pytest.raises(Forbidden) as transport:
        gateway._require_secure_transport(
            OpenAICompatibleConfig(base_url=base_url.replace("https", "http"), model="m")
        )
    assert SECRET not in str(transport.value)


class _FakeAdmissionTransport:
    """Stands in for ``ollama_endpoint.open_url``: control-plane reads fail, chat succeeds."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request, timeout=None, **_kwargs):
        self.urls.append(request.full_url)
        if request.get_method() == "POST" and request.full_url.endswith("/api/chat"):
            return io.BytesIO(json.dumps({"message": {"content": "ok"}}).encode("utf-8"))
        raise urllib.error.URLError("control plane down: " + request.full_url)


def test_vision_post_through_real_admission_does_not_log_credentials(
    caplog, monkeypatch, tmp_path
):
    caplog.set_level(logging.DEBUG)
    reading = HostMemoryReading(
        available_physical_bytes=64 * 2**30, total_physical_bytes=64 * 2**30,
        commit_limit_bytes=96 * 2**30, commit_available_bytes=64 * 2**30, commit_enforced=True,
    )
    service = gpu_admission.GpuAdmission(
        GpuAdmissionConfig(mode="enforce", store_path=str(tmp_path / "gpu.sqlite3")),
        host_reader=lambda: reading, memory_reader=lambda _key: None,
        show_reader=lambda _origin, _model: None, background=lambda work: work(),
    )
    gpu_admission.set_process_service(service)
    fake = _FakeAdmissionTransport()
    monkeypatch.setattr(ollama_endpoint, "open_url", fake)
    payload = {"model": "llava-local", "messages": [], "options": {"num_ctx": 4096}}
    data = ollama_vision.OllamaVisionGateway._post(
        "http://user:s3cret@127.0.0.1:11434/api/chat", payload, 5.0
    )
    assert data == {"message": {"content": "ok"}}
    assert any(url.endswith("/api/ps") for url in fake.urls), "admission never read /api/ps"
    _assert_emitted(caplog, "gpu admission could not read")
    _assert_clean(caplog)


def test_openai_compat_generate_does_not_log_base_url_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    gateway, cfg = _gateway(lambda url, payload, headers, timeout: {"ok": True})
    try:
        gateway.generate(
            type("R", (), {"options": {}, "tier": "t", "prompt": "p", "system": None,
                           "messages": None})(),
            local_owner_context(correlation_id="redact"),
        )
    except Exception:
        pass
    assert any("OpenAICompatibleGateway.generate" in r.getMessage() for r in caplog.records)
    _assert_clean(caplog)
