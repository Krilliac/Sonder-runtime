"""Host-configured request velocity blocks physical model sends and refills."""

import urllib.error

import pytest

import server
from sonder_runtime.adapters import model_request_admission
from sonder_runtime.adapters.model_error_formatting import format_model_call_error
from sonder_runtime.adapters.model_transport import ModelCallError


def test_rate_policy_is_disabled_without_explicit_host_startup_config():
    admission = model_request_admission.HostModelRequestAdmission.from_environ({})
    assert admission.enabled is False
    with pytest.raises(ValueError, match="both"):
        model_request_admission.HostModelRequestAdmission.from_environ(
            {"SONDER_MODEL_REQUEST_BURST": "2"}
        )
    with pytest.raises(ValueError, match="ceiling"):
        model_request_admission.HostModelRequestAdmission.from_environ(
            {"SONDER_MODEL_REQUEST_BURST": "99999",
             "SONDER_MODEL_REQUESTS_PER_MINUTE": "2"}
        )


def test_physical_post_attempts_are_bounded_even_across_independent_calls(
    monkeypatch, tmp_path,
):
    now = [100.0]
    policy = model_request_admission.HostModelRequestAdmission.from_environ(
        {"SONDER_MODEL_REQUEST_BURST": "2",
         "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
        clock=lambda: now[0],
        db_path=tmp_path / "rate.db",
    )
    monkeypatch.setattr(server, "_HOST_MODEL_REQUEST_ADMISSION", policy)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_: None)
    requests = []

    def provider(path, payload, **kwargs):
        requests.append((path, payload))
        return {"message": {"content": "ok"}, "done": True}

    monkeypatch.setattr(server, "_post", provider)
    payload = {"model": "sonder:latest", "messages": [{"role": "user", "content": "p"}]}
    for _ in range(2):
        assert server._post_model("/api/chat", payload, model="sonder:latest")[1] == 1
    with pytest.raises(ModelCallError, match="request rate") as blocked:
        server._post_model("/api/chat", payload, model="sonder:latest")
    assert blocked.value.kind == "rate"
    assert blocked.value.attempts == 0
    assert blocked.value.retry_after_seconds > 0
    assert "Retry after about" in format_model_call_error(
        blocked.value, target="local Ollama", display="loopback"
    )
    assert len(requests) == 2
    now[0] += blocked.value.retry_after_seconds + 0.001
    server._post_model("/api/chat", payload, model="sonder:latest")
    assert len(requests) == 3


def test_fanout_target_generator_obeys_host_request_admission(monkeypatch, tmp_path):
    policy = model_request_admission.HostModelRequestAdmission.from_environ(
        {"SONDER_MODEL_REQUEST_BURST": "1",
         "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
        clock=lambda: 100.0,
        db_path=tmp_path / "rate.db",
    )
    monkeypatch.setattr(server, "_HOST_MODEL_REQUEST_ADMISSION", policy)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_: None)
    requests = []

    def provider(path, payload, **kwargs):
        requests.append(payload)
        return {"message": {"content": "ok"}, "done": True}

    monkeypatch.setattr(server, "_post", provider)
    # Durable fanout dispatch creates this same generator for each selected
    # target; a second run cannot reset the host's physical-request bucket.
    gen = server._make_generate("sonder:latest", "", 0.2, 100, 4096)
    assert gen("first") == "ok"
    with pytest.raises(ModelCallError, match="request rate"):
        server._make_generate("sonder:latest", "", 0.2, 100, 4096)("second")
    assert len(requests) == 1


def test_provider_retry_is_charged_as_another_physical_request(monkeypatch, tmp_path):
    policy = model_request_admission.HostModelRequestAdmission.from_environ(
        {"SONDER_MODEL_REQUEST_BURST": "1",
         "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
        clock=lambda: 100.0,
        db_path=tmp_path / "rate.db",
    )
    monkeypatch.setattr(server, "_HOST_MODEL_REQUEST_ADMISSION", policy)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_: None)
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "0")
    calls = []

    def provider(_path, _payload, **_kwargs):
        calls.append(1)
        raise urllib.error.URLError(ConnectionResetError("retryable"))

    monkeypatch.setattr(server, "_post", provider)
    with pytest.raises(ModelCallError, match="request rate") as blocked:
        server._post_model("/api/chat", {}, model="sonder:latest", timeout=5)
    assert blocked.value.kind == "rate"
    assert blocked.value.attempts == 1
    assert calls == [1]
