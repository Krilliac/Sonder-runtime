"""SonderInferenceGateway: the Sonder Inference HTTP API v1 as a ModelGateway.

Most tests inject the shared transport seams (no network).  The tests at the
end drive the stdlib urllib transports against a real loopback HTTP server
started in-process, so refused connections, error bodies and 503 health
documents are exercised through the same code the runtime uses.
"""
from __future__ import annotations

import io
import json
import logging
import socket
import threading
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    STATUS_KEYS,
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
    config_from_env,
    correlation_headers,
    direct_get_transport,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.ports.model_gateway_contract import Capability
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)
from sonder_runtime.domain.routing.backend_conformance import BackendIdentity

HEX = "a" * 64
IDENTITY = {
    "backend": "mock", "model": "mock", "model_digest": HEX, "quantization": "none",
    "backend_version": "0.1.0", "tokenizer_digest": "b" * 64,
    "template_digest": "c" * 64, "context_tokens": 4096, "hardware": "cpu",
}


def _health(status="ready", *, api_version=1, synthetic=False, models=("mock",)):
    return {
        "status": status, "api_version": api_version, "version": "0.1.0",
        "commit": "912503a", "abi_version": 1, "instance_id": "tel-1", "node_id": "host",
        "uptime_s": 3, "synthetic": synthetic, "auth_required": False,
        "backends": [{"name": "mock", "available": True, "capabilities": ["chat"]}],
        "models": [{"id": m, "backend": "mock", "default": i == 0} for i, m in enumerate(models)],
        "telemetry": {"level": "standard", "subscribers": 0, "retained": 0,
                      "capacity": 8192, "emitted": 0, "dropped": 0},
    }


def _chat(text="hello", *, model="mock", api_version=1, usage=True):
    body = {
        "id": "chatcmpl-r1", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "timings": {"prompt_n": 4, "predicted_n": 6, "total_ms": 30.0, "ttft_ms": 5.0,
                    "prompt_ms": 10.0, "predicted_ms": 20.0},
        "sonder": {"request_id": "r1", "session_id": "s1", "backend": "mock",
                   "synthetic": True, "token_counts_from_backend": True,
                   "api_version": api_version},
    }
    if usage:
        body["usage"] = {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    return body


def _http_error(url, status, code="", message="boom"):
    body = json.dumps({"error": {"message": message, "type": "x", "code": code,
                                 "param": None}}).encode()
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(body))


class FakeInference:
    """Scriptable GET/POST seams that record everything sent."""

    def __init__(self, *, health=None, health_status=200, identity=None,
                 identity_status=200, chat=None):
        self.health = _health() if health is None else health
        self.health_status = health_status
        self.identity = identity
        self.identity_status = identity_status
        self.chat = chat if chat is not None else (lambda url, payload, headers: _chat())
        self.gets: list[tuple[str, dict, float]] = []
        self.posts: list[tuple[str, dict, dict, float]] = []
        self.get_error: BaseException | None = None

    def get(self, url, headers, timeout):
        self.gets.append((url, dict(headers), timeout))
        if self.get_error is not None:
            raise self.get_error
        if "/v1/sonder/health" in url:
            return self.health_status, json.dumps(self.health).encode()
        if "/v1/sonder/identity" in url:
            document = self.identity if self.identity is not None else {
                "schema": "sonder.inference.identity/1", "model": "mock",
                "synthetic": True, "backend_identity": IDENTITY, "reason": None,
            }
            return self.identity_status, json.dumps(document).encode()
        raise AssertionError(url)

    def post(self, url, payload, headers, timeout):
        self.posts.append((url, json.loads(json.dumps(payload)), dict(headers), timeout))
        result = self.chat(url, payload, headers)
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _gateway(fake, config=None, *, clock=None, **settings):
    cfg = config or SonderInferenceConfig(**settings)
    return SonderInferenceGateway(
        cfg, transport=fake.post, get_transport=fake.get,
        monotonic=clock or Clock(),
        wall_clock=lambda: datetime(2026, 9, 26, 12, 0, 0, 123000, tzinfo=timezone.utc),
    )


def _ctx(*, correlation="turn-1", source="http", cloud=False, timeout=30.0, cancellation=None):
    return local_owner_context(
        correlation_id=correlation, source=source, cloud_allowed=cloud,
        timeout_seconds=timeout, cancellation=cancellation,
    )


# -- request shape, headers, response --------------------------------------


def test_body_is_the_openai_subset_and_never_an_ollama_model_name():
    fake = FakeInference()
    gateway = _gateway(fake, model="default")
    response = gateway.generate(
        ModelRequest(
            prompt="hi", tier="general", system="be terse",
            history=({"role": "user", "content": "a"}, ("assistant", "b")),
            options={"temperature": 0.1, "num_predict": 32, "num_ctx": 2048,
                     "top_k": 40, "seed": 7, "stop": "END", "think": False},
        ),
        _ctx(),
    )
    url, payload, _headers, _timeout = fake.posts[0]
    assert url == DEFAULT_BASE_URL + "/v1/chat/completions"
    assert payload == {
        "model": "default",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "hi"},
        ],
        "stream": False,
        "temperature": 0.1, "max_tokens": 32, "num_ctx": 2048, "top_k": 40,
        "seed": 7, "stop": ["END"],
    }
    assert "sonder:latest" not in json.dumps(payload)
    # The served model comes from the response, never the request alias.
    assert response.model == "mock"
    assert response.text == "hello"
    assert (response.tokens_in, response.tokens_out) == (4, 6)
    assert response.telemetry.prompt_eval_ms == 10.0
    assert response.telemetry.eval_ms == 20.0
    assert response.telemetry.backend_total_ms == 30.0
    assert response.telemetry.output_tokens_per_second == pytest.approx(300.0)


def test_usage_falls_back_to_timings_counts():
    fake = FakeInference(chat=lambda *a: _chat(usage=False))
    response = _gateway(fake).generate(ModelRequest(prompt="hi", tier="fast"), _ctx())
    assert (response.tokens_in, response.tokens_out) == (4, 6)


def test_model_selection_prefers_option_then_tier_map_then_default():
    fake = FakeInference()
    gateway = _gateway(fake, model="general-m", tier_models={"fast": "fast-m"})
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    gateway.generate(ModelRequest(prompt="x", tier="code"), _ctx())
    gateway.generate(ModelRequest(prompt="x", tier="fast", options={"model": "explicit"}), _ctx())
    assert [post[1]["model"] for post in fake.posts] == ["fast-m", "general-m", "explicit"]


@pytest.mark.parametrize("options", [
    {"format": "json"}, {"tools": [{"type": "function"}]},
    {"response_format": {"type": "json_object"}}, {"temperature": "hot"},
    {"stop": ["a", "b", "c", "d", "e"]}, {"num_predict": 1.5}, {"top_k": True},
])
def test_unsupported_or_invalid_options_are_refused_before_any_send(options):
    fake = FakeInference()
    with pytest.raises(InvalidInput):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast", options=options), _ctx())
    assert fake.posts == [] and fake.gets == []


def test_thinking_is_refused_before_any_prompt_when_the_server_does_not_advertise_it():
    # Support is read from the (cached) health document, so the refusal now
    # follows one health GET; the prompt is still never sent.
    fake = FakeInference()
    with pytest.raises(InvalidInput, match="thinking"):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast", options={"think": True}), _ctx())
    assert fake.posts == []


def test_correlation_workload_and_auth_headers():
    fake = FakeInference()
    gateway = _gateway(fake)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(correlation="turn:1.a-b"))
    headers = fake.posts[0][2]
    assert headers["X-Sonder-Parent-Request-Id"] == "turn:1.a-b"
    assert headers["X-Sonder-Run-Id"] == "turn:1.a-b"
    assert headers["X-Sonder-Workload"] == "interactive_user"
    assert "Authorization" not in headers
    assert all("Authorization" not in get[1] for get in fake.gets)

    keyed = FakeInference()
    _gateway(keyed, api_key="k-123").generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert keyed.posts[0][2]["Authorization"] == "Bearer k-123"
    assert keyed.gets[0][1]["Authorization"] == "Bearer k-123"


@pytest.mark.parametrize("source,workload", [
    ("http", "interactive_user"), ("repl", "interactive_user"),
    ("mcp", "owner_orchestrator"), ("worker", "implementation_worker"),
    ("system", "maintenance"),
])
def test_workload_mapping_from_context_source(source, workload):
    assert correlation_headers(_ctx(source=source))["X-Sonder-Workload"] == workload


@pytest.mark.parametrize("correlation", ["has space", "x" * 129, "", "turn/1", "ü"])
def test_invalid_correlation_ids_are_omitted_not_rewritten(correlation):
    headers = correlation_headers(_ctx(correlation=correlation))
    assert "X-Sonder-Parent-Request-Id" not in headers
    assert "X-Sonder-Run-Id" not in headers


def test_response_must_name_the_resolved_model():
    for body in (_chat(model="default"), {**_chat(), "model": None}):
        fake = FakeInference(chat=lambda *a, body=body: body)
        with pytest.raises(DependencyUnavailable, match="served model"):
            _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())


def test_body_api_version_mismatch_is_refused():
    fake = FakeInference(chat=lambda *a: _chat(api_version=2))
    with pytest.raises(DependencyUnavailable, match="incompatible sonder-inference API") as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)


# -- error mapping (contract 2.7) ---------------------------------------------


@pytest.mark.parametrize("status,code,expected", [
    (400, "invalid_json", InvalidInput),
    (400, "unsupported_parameter", InvalidInput),
    (400, "invalid_correlation_header", InvalidInput),
    (401, "unauthorized", Forbidden),
    (403, "forbidden_origin", Forbidden),
    (403, "forbidden_host", Forbidden),
    (404, "model_not_found", InvalidInput),
    (405, "", InvalidInput),
    (408, "", DependencyUnavailable),
    (411, "", InvalidInput),
    (413, "", InvalidInput),
    (429, "overloaded", CapacityExceeded),
    (500, "internal_error", DependencyUnavailable),
    (501, "not_implemented", InvalidInput),
    (503, "overloaded", CapacityExceeded),
    (503, "backend_unavailable", DependencyUnavailable),
    (503, "not_ready", SonderInferenceUnreachable),
])
def test_every_documented_status_maps_to_one_domain_error(status, code, expected):
    fake = FakeInference(chat=lambda url, *a: _http_error(url, status, code, "detail text"))
    with pytest.raises(expected) as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    if expected is not SonderInferenceUnreachable:
        assert not isinstance(info.value, SonderInferenceUnreachable)
    assert len(fake.posts) == 1  # single attempt, never retried


def test_error_code_is_dependency_unavailable_for_capture():
    assert SonderInferenceUnreachable.code == DependencyUnavailable.code


# -- pre-send classification --------------------------------------------------


@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    ConnectionRefusedError(111, "refused"),
    urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
])
def test_connect_failures_on_send_are_unreachable(error):
    fake = FakeInference(chat=lambda *a: error)
    with pytest.raises(SonderInferenceUnreachable):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())


@pytest.mark.parametrize("error,expected", [
    (socket.timeout("timed out"), DeadlineExceeded),
    (urllib.error.URLError(TimeoutError()), DeadlineExceeded),
    (ConnectionResetError(104, "reset"), DependencyUnavailable),
])
def test_post_send_failures_are_never_unreachable(error, expected):
    fake = FakeInference(chat=lambda *a: error)
    with pytest.raises(expected) as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)


def test_down_server_message_names_url_command_and_fallback():
    fake = FakeInference()
    fake.get_error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    with pytest.raises(SonderInferenceUnreachable) as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    message = str(info.value)
    assert DEFAULT_BASE_URL in message
    assert "sonder-infer serve" in message
    assert "SONDER_INFERENCE_FALLBACK" in message
    assert fake.posts == []


def test_cached_unhealthy_state_refuses_without_sending_and_expires_with_ttl():
    clock = Clock()
    fake = FakeInference(health=_health("starting"), health_status=503)
    gateway = _gateway(fake, clock=clock, health_ttl_seconds=5.0)
    for _ in range(3):
        with pytest.raises(SonderInferenceUnreachable, match="starting"):
            gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert len(fake.gets) == 1 and fake.posts == []
    fake.health, fake.health_status = _health("ready"), 200
    clock.now += 4.9
    with pytest.raises(SonderInferenceUnreachable):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    clock.now += 0.2
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "hello"
    assert len(fake.gets) == 2 and len(fake.posts) == 1


def test_send_time_refusal_marks_cache_unavailable():
    clock = Clock()
    calls = []

    def chat(*_):
        calls.append(1)
        return urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    fake = FakeInference(chat=chat)
    gateway = _gateway(fake, clock=clock)
    with pytest.raises(SonderInferenceUnreachable):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    with pytest.raises(SonderInferenceUnreachable):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert len(calls) == 1  # the second refusal came from the cache


def test_health_probe_timeout_is_bounded_to_five_seconds():
    fake = FakeInference()
    gateway = _gateway(fake, timeout_seconds=300.0)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(timeout=120.0))
    assert fake.gets[0][2] == 5.0
    assert fake.posts[0][3] <= 120.0


def test_health_api_version_mismatch_is_not_unreachable():
    fake = FakeInference(health=_health(api_version=2))
    with pytest.raises(DependencyUnavailable, match="incompatible") as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)
    assert fake.posts == []


def test_rejected_credentials_on_health_are_forbidden_not_unreachable():
    fake = FakeInference(health={"error": {"code": "unauthorized"}}, health_status=401)
    with pytest.raises(Forbidden, match="SONDER_INFERENCE_API_KEY"):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())


def test_cancellation_and_deadline_are_checked_before_any_send():
    class Cancelled:
        cancelled = True

        def wait(self, timeout=None):
            return True

    fake = FakeInference()
    from sonder_runtime.domain.common.errors import Cancelled as CancelledError

    with pytest.raises(CancelledError):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=Cancelled()))
    with pytest.raises(DeadlineExceeded):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx(timeout=0.0))
    assert fake.posts == [] and fake.gets == []


# -- consent --------------------------------------------------------------------


@pytest.mark.parametrize("settings,cloud,match", [
    ({"base_url": "https://gpu.example:11437", "api_key": "k"}, True, "SONDER_ALLOW_REMOTE_INFERENCE"),
    ({"base_url": "http://gpu.example:11437", "api_key": "k", "allow_remote": True}, True, "https"),
    ({"base_url": "https://gpu.example:11437", "allow_remote": True}, True, "SONDER_INFERENCE_API_KEY"),
    ({"base_url": "https://gpu.example:11437", "api_key": "k", "allow_remote": True}, False,
     "does not allow prompts"),
])
def test_remote_endpoint_is_refused_before_any_send(settings, cloud, match):
    fake = FakeInference()
    with pytest.raises(Forbidden, match=match):
        _gateway(fake, **settings).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cloud=cloud))
    assert fake.posts == [] and fake.gets == []


def test_remote_endpoint_with_full_consent_is_used():
    fake = FakeInference()
    gateway = _gateway(fake, base_url="https://gpu.example:11437/infer", api_key="k",
                       allow_remote=True)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(cloud=True))
    assert fake.posts[0][0] == "https://gpu.example:11437/infer/v1/chat/completions"
    status = gateway.provider_status()["sonder_inference"]
    assert status["base_url"] == "https://gpu.example:11437"


@pytest.mark.parametrize("url", ["http://127.0.0.1:1", "http://localhost:2", "http://[::1]:3"])
def test_loopback_needs_no_consent(url):
    fake = FakeInference()
    _gateway(fake, base_url=url).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert len(fake.posts) == 1


@pytest.mark.parametrize("url", ["http://127.1.2.3:4", "http://127.0.0.2:11437"])
def test_loopback_aliases_inference_would_refuse_are_configuration_errors(url):
    # Inference's Host check (contract 2.3) accepts only 127.0.0.1, localhost
    # and [::1]; any other loopback alias would pass consent and then fail
    # every request with a misleading 403.
    with pytest.raises(InvalidInput, match="127.0.0.1, localhost or \\[::1\\]"):
        SonderInferenceConfig(base_url=url)


def test_ipv6_loopback_spellings_normalize_to_one_host():
    config = SonderInferenceConfig(base_url="http://[0:0:0:0:0:0:0:1]:11437")
    assert config.base_url == "http://[::1]:11437" and config.loopback


def test_bind_all_addresses_are_rewritten_to_loopback():
    assert SonderInferenceConfig(base_url="http://0.0.0.0:11437/").base_url == "http://127.0.0.1:11437"
    assert SonderInferenceConfig(base_url="http://[::]:11437").base_url == "http://[::1]:11437"


@pytest.mark.parametrize("url", ["ftp://127.0.0.1", "http://u:p@127.0.0.1", "http://127.0.0.1/?q=1",
                                 "127.0.0.1:11437", "http://127.0.0.1:99999"])
def test_malformed_base_urls_are_configuration_errors(url):
    with pytest.raises(InvalidInput):
        SonderInferenceConfig(base_url=url)


# -- configuration --------------------------------------------------------------


def test_env_defaults_and_overrides():
    assert config_from_env({}) == SonderInferenceConfig(base_url_source="default")
    cfg = config_from_env({
        "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:18437",
        "SONDER_INFERENCE_MODEL": "m1",
        "SONDER_INFERENCE_TIER_MODELS": "fast=a, general=b",
        "SONDER_INFERENCE_API_KEY": "k",
        "SONDER_INFERENCE_TIMEOUT_SECONDS": "12.5",
        "SONDER_INFERENCE_HEALTH_TTL_SECONDS": "0",
    })
    assert cfg.base_url == "http://127.0.0.1:18437"
    assert cfg.model == "m1"
    assert dict(cfg.tier_models) == {"fast": "a", "general": "b"}
    assert (cfg.timeout_seconds, cfg.health_ttl_seconds) == (12.5, 0.0)
    assert cfg.base_url_source == "env"


@pytest.mark.parametrize("env", [
    {"SONDER_INFERENCE_TIER_MODELS": "turbo=a"},
    {"SONDER_INFERENCE_TIER_MODELS": "fast"},
    {"SONDER_INFERENCE_TIMEOUT_SECONDS": "0"},
    {"SONDER_INFERENCE_TIMEOUT_SECONDS": "soon"},
    {"SONDER_INFERENCE_HEALTH_TTL_SECONDS": "-1"},
    {"SONDER_ALLOW_REMOTE_INFERENCE": "yes"},
    {"SONDER_INFERENCE_MODEL": "has space"},
])
def test_invalid_env_values_fail_closed(env):
    with pytest.raises(InvalidInput):
        config_from_env(env)


def test_ready_file_is_used_only_without_a_base_url(tmp_path):
    ready = tmp_path / "ready.json"
    ready.write_text(json.dumps({"url": "http://127.0.0.1:40111", "pid": 1,
                                 "instance_id": "tel-x", "api_version": 1}))
    cfg = config_from_env({"SONDER_INFERENCE_READY_FILE": str(ready)})
    assert (cfg.base_url, cfg.base_url_source) == ("http://127.0.0.1:40111", "ready_file")
    explicit = config_from_env({"SONDER_INFERENCE_READY_FILE": str(ready),
                                "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:1"})
    assert explicit.base_url == "http://127.0.0.1:1"


def test_ready_file_edge_cases(tmp_path):
    with pytest.raises(SonderInferenceUnreachable, match="does not exist"):
        config_from_env({"SONDER_INFERENCE_READY_FILE": str(tmp_path / "missing.json")})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_INFERENCE_READY_FILE": str(bad)})
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"url": "http://127.0.0.1:1", "api_version": 2}))
    with pytest.raises(DependencyUnavailable, match="incompatible"):
        config_from_env({"SONDER_INFERENCE_READY_FILE": str(future)})


def test_env_is_read_lazily_per_call(monkeypatch):
    fake = FakeInference()
    gateway = SonderInferenceGateway(transport=fake.post, get_transport=fake.get)
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:18437")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "late")
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert fake.posts[0][0].startswith("http://127.0.0.1:18437/")
    assert fake.posts[0][1]["model"] == "late"


# -- health, identity and status ---------------------------------------------------


def test_capability_health_is_cached_and_never_generates():
    fake = FakeInference()
    gateway = _gateway(fake)
    health = gateway.capability_health()
    assert health.provider == "sonder_inference"
    assert health.healthy is True and health.supports(Capability.GENERATION)
    assert not health.supports(Capability.STREAMING)
    gateway.capability_health()
    assert len(fake.gets) == 1 and fake.posts == []

    down = FakeInference()
    down.get_error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    unhealthy = _gateway(down).capability_health()
    assert unhealthy.healthy is False and "refused" in unhealthy.detail


def test_identity_is_parsed_exactly_and_synthetic_is_never_routing_evidence():
    fake = FakeInference()
    gateway = _gateway(fake)
    identity = gateway.backend_identity()
    assert identity == BackendIdentity.from_dict(IDENTITY)
    observation = gateway.observe_identity()
    assert observation.synthetic is True
    assert gateway.routing_identity() is None

    real = FakeInference(identity={
        "schema": "sonder.inference.identity/1", "model": "m", "synthetic": False,
        "backend_identity": {**IDENTITY, "backend": "ollama", "model": "m"}, "reason": None,
    })
    assert _gateway(real).routing_identity() == BackendIdentity.from_dict(
        {**IDENTITY, "backend": "ollama", "model": "m"}
    )


@pytest.mark.parametrize("backend_identity", [
    {**IDENTITY, "extra": 1},
    {key: value for key, value in IDENTITY.items() if key != "hardware"},
    {**IDENTITY, "model_digest": "A" * 64},
    {**IDENTITY, "tokenizer_digest": "a" * 63},
    {**IDENTITY, "context_tokens": 0},
])
def test_identity_requires_nine_keys_and_lowercase_sha256(backend_identity):
    fake = FakeInference(identity={
        "schema": "sonder.inference.identity/1", "model": "m", "synthetic": False,
        "backend_identity": backend_identity, "reason": None,
    })
    with pytest.raises(DependencyUnavailable, match="invalid backend identity"):
        _gateway(fake).backend_identity()


def test_unmeasurable_identity_is_null_with_a_reason_and_model_is_queried():
    fake = FakeInference(identity={
        "schema": "sonder.inference.identity/1", "model": "llama", "synthetic": False,
        "backend_identity": None, "reason": "tokenizer digest not measurable via ollama",
    })
    gateway = _gateway(fake)
    observation = gateway.observe_identity("llama 3")
    assert observation.identity is None
    assert "tokenizer" in observation.reason
    assert fake.gets[-1][0].endswith("/v1/sonder/identity?model=llama%203")
    wrong_schema = FakeInference(identity={"schema": "other/1"})
    with pytest.raises(DependencyUnavailable, match="schema"):
        _gateway(wrong_schema).backend_identity()


def test_provider_status_keys_and_types_when_ready():
    fake = FakeInference(health=_health(synthetic=True, models=("mock", "other")))
    status = _gateway(fake).provider_status()
    assert list(status) == ["sonder_inference"]
    entry = status["sonder_inference"]
    assert tuple(entry) == STATUS_KEYS
    assert entry == {
        "provider": "sonder_inference",
        "state": "ready",
        "healthy": True,
        "checked_at": "2026-09-26T12:00:00.123Z",
        "detail": "ready: 2 model(s), synthetic mock backend",
        "capabilities": ["chat", "fixed-endpoint"],
        "base_url": DEFAULT_BASE_URL,
        "version": "0.1.0",
        "api_version": 1,
        "models": ["mock", "other"],
        "synthetic": True,
        "identity": IDENTITY,
        "telemetry": {
            "discovery_url": DEFAULT_BASE_URL + "/.well-known/sonder-telemetry",
            "sse_url": DEFAULT_BASE_URL + "/v1/telemetry/sse",
            "ndjson_url": DEFAULT_BASE_URL + "/v1/telemetry/ndjson",
        },
        "fallback": None,
        "fallback_count": 0,
        "tier_models": {tier: DEFAULT_MODEL for tier in
                        ("fast", "general", "code", "reasoning", "vision")},
        "busy": False,
        "workers": None,
    }


def test_provider_status_when_down_degraded_or_misconfigured():
    down = FakeInference()
    down.get_error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    entry = _gateway(down).provider_status()["sonder_inference"]
    assert (entry["state"], entry["healthy"], entry["telemetry"], entry["identity"]) == (
        "unavailable", False, None, None)
    assert isinstance(entry["checked_at"], str) and len(entry["detail"]) <= 240

    starting = FakeInference(health=_health("draining"), health_status=503)
    entry = _gateway(starting).provider_status()["sonder_inference"]
    assert entry["state"] == "degraded" and entry["telemetry"] is not None
    assert entry["identity"] is None

    remote = FakeInference()
    entry = _gateway(remote, base_url="https://gpu.example").provider_status()["sonder_inference"]
    assert entry["state"] == "unavailable" and "SONDER_ALLOW_REMOTE_INFERENCE" in entry["detail"]
    assert remote.gets == []


def test_embeddings_are_refused_with_the_fix():
    with pytest.raises(DependencyUnavailable, match="SONDER_EMBEDDING_PROVIDER=ollama"):
        _gateway(FakeInference()).embed(["x"], _ctx())


def test_capabilities_advertise_chat_only():
    assert _gateway(FakeInference()).capabilities == frozenset({"chat", "fixed-endpoint"})


def test_served_tier_models_resolve_each_tier_without_io():
    fake = FakeInference()
    gateway = _gateway(fake, model="qwen3:14b", tier_models={"reasoning": "deepseek-r1:14b"})
    assert gateway.served_tier_models() == {"sonder_inference": {
        "fast": "qwen3:14b", "general": "qwen3:14b", "code": "qwen3:14b",
        "reasoning": "deepseek-r1:14b", "vision": "qwen3:14b",
    }}
    assert fake.gets == [] and fake.posts == []


def test_provider_status_reports_tier_models_even_when_down():
    down = FakeInference()
    down.get_error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    entry = _gateway(
        down, model="qwen3:14b", tier_models={"reasoning": "deepseek-r1:14b"},
    ).provider_status()["sonder_inference"]
    assert entry["state"] == "unavailable"
    assert entry["tier_models"]["reasoning"] == "deepseek-r1:14b"
    assert entry["tier_models"]["general"] == "qwen3:14b"


# -- real loopback HTTP through the stdlib transports ------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # keep test output clean
        return

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Sonder-Inference-Api", "1")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.seen.append(("GET", self.path, dict(self.headers)))
        status, body = self.routes[self.path.split("?")[0]]
        self._reply(status, body)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.seen.append(("POST", self.path, dict(self.headers), payload))
        status, body = self.routes[self.path]
        self._reply(status, body)


@pytest.fixture
def loopback_server():
    handler = type("Handler", (_Handler,), {"routes": {}, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        server.server_close()


def test_real_http_round_trip_and_not_ready_error_body(loopback_server):
    server, handler = loopback_server
    base = "http://127.0.0.1:%d" % server.server_address[1]
    handler.routes.update({
        "/v1/sonder/health": (200, _health()),
        "/v1/chat/completions": (200, _chat("real")),
    })
    # Real stdlib GET transport, with a budget that cannot expire spuriously.
    # The gateway caps health probes at 5s, and a loopback probe starved past
    # that under CPU load (measured 5.17s) takes the designed busy fallback:
    # the recent "ready" observation is reused, labelled busy, so the final
    # "degraded" assertion saw "ready".  That fallback has its own tests
    # (test_a3_*); this test is about the HTTP round trip and its mapping.
    # 60s is a hang detector here, not a latency expectation.
    def unhurried_get(url, headers, _timeout):
        return direct_get_transport(url, headers, 60.0)

    gateway = SonderInferenceGateway(
        SonderInferenceConfig(base_url=base, health_ttl_seconds=0), get_transport=unhurried_get,
    )
    response = gateway.generate(ModelRequest(prompt="hi", tier="fast"), _ctx(correlation="r-1"))
    assert (response.text, response.model) == ("real", "mock")
    post = [entry for entry in handler.seen if entry[0] == "POST"][0]
    assert post[2]["X-Sonder-Run-Id"] == "r-1"
    assert post[3]["model"] == "default"

    handler.routes["/v1/chat/completions"] = (
        503, {"error": {"message": "warming", "type": "service_unavailable",
                        "code": "not_ready", "param": None}},
    )
    with pytest.raises(SonderInferenceUnreachable):
        gateway.generate(ModelRequest(prompt="hi", tier="fast"), _ctx())
    handler.routes["/v1/chat/completions"] = (
        503, {"error": {"message": "backend died", "type": "service_unavailable",
                        "code": "backend_unavailable", "param": None}},
    )
    with pytest.raises(DependencyUnavailable, match="backend died") as info:
        gateway.generate(ModelRequest(prompt="hi", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)

    handler.routes["/v1/sonder/health"] = (503, _health("starting"))
    status = gateway.provider_status()["sonder_inference"]
    assert (status["state"], status["busy"]) == ("degraded", False), status["detail"]


def test_real_refused_port_is_unreachable_and_logged(caplog):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    gateway = SonderInferenceGateway(SonderInferenceConfig(base_url="http://127.0.0.1:%d" % port))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SonderInferenceUnreachable) as info:
            gateway.generate(ModelRequest(prompt="hi", tier="fast"), _ctx())
    assert "127.0.0.1:%d" % port in str(info.value)
    assert gateway.provider_status()["sonder_inference"]["state"] == "unavailable"


def test_api_key_is_a_redacted_secret_and_never_logged(monkeypatch, caplog):
    from sonder_runtime.platform import logging as runtime_logging

    assert "SONDER_INFERENCE_API_KEY" in runtime_logging.SECRET_ENV_VARS
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY", "sk-inference-secret-value")
    assert "sk-inference-secret-value" not in runtime_logging.Redactor().redact(
        "token sk-inference-secret-value"
    )
    fake = FakeInference(chat=lambda url, *a: _http_error(url, 401, "unauthorized"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Forbidden):
            _gateway(fake, api_key="sk-inference-secret-value").generate(
                ModelRequest(prompt="x", tier="fast"), _ctx(),
            )
    assert "sk-inference-secret-value" not in caplog.text


# -- transport hardening: proxies, redirects, garbage, trickle, overload ----------


def _serve_raw(handler_fn):
    """Start a raw TCP listener; ``handler_fn(conn)`` answers each connection."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    stop = threading.Event()

    def loop():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=_answer, args=(conn,), daemon=True).start()

    def _answer(conn):
        try:
            handler_fn(conn)
        except OSError:
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(2)
        listener.close()

    return listener.getsockname()[1], close


def _read_request(conn) -> bytes:
    conn.settimeout(2)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def _http_response(status, body: dict, extra=b"") -> bytes:
    raw = json.dumps(body).encode()
    return (b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
            b"Connection: close\r\n%s\r\n" % (status, len(raw), extra)) + raw


@pytest.fixture
def recording_proxy(monkeypatch):
    """A 'proxy' that records every request and answers as if it were Inference."""
    seen = []

    def handle(conn):
        request = _read_request(conn)
        seen.append(request)
        body = _health() if b"/v1/sonder/health" in request.split(b"\r\n")[0] else _chat("via proxy")
        conn.sendall(_http_response(200, body))

    port, close = _serve_raw(handle)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:%d" % port)
    yield seen
    close()


def _refused_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_environment_proxies_never_see_inference_traffic(recording_proxy, loopback_server):
    # Down server: without the fix the proxy answered for it, leaking the
    # prompt and key and hiding the outage from the pre-send classifier.
    down = SonderInferenceGateway(SonderInferenceConfig(
        base_url="http://127.0.0.1:%d" % _refused_port(), api_key="sk-secret-key",
    ))
    with pytest.raises(SonderInferenceUnreachable):
        down.generate(ModelRequest(prompt="PRIVATE PROMPT", tier="fast"), _ctx())
    # Live server: the request goes straight to it.
    server, handler = loopback_server
    handler.routes.update({
        "/v1/sonder/health": (200, _health()),
        "/v1/chat/completions": (200, _chat("direct")),
    })
    live = SonderInferenceGateway(SonderInferenceConfig(
        base_url="http://127.0.0.1:%d" % server.server_address[1], api_key="sk-secret-key",
    ))
    assert live.generate(ModelRequest(prompt="PRIVATE PROMPT", tier="fast"), _ctx()).text == "direct"
    assert recording_proxy == []


def test_redirects_are_never_followed_and_never_carry_the_key(loopback_server):
    stolen = []

    def thief(conn):
        stolen.append(_read_request(conn))
        conn.sendall(_http_response(200, _health()))

    thief_port, close = _serve_raw(thief)
    try:
        server, handler = loopback_server
        target = "http://127.0.0.1:%d/steal" % thief_port

        class Redirecting(handler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET

        server.RequestHandlerClass = Redirecting
        gateway = SonderInferenceGateway(SonderInferenceConfig(
            base_url="http://127.0.0.1:%d" % server.server_address[1], api_key="sk-secret-key",
        ))
        snapshot = gateway.health(refresh=True)
        assert snapshot.state == "unavailable" and "302" in snapshot.detail
        post_only = SonderInferenceGateway(
            SonderInferenceConfig(base_url="http://127.0.0.1:%d" % server.server_address[1],
                                  api_key="sk-secret-key"),
            get_transport=FakeInference().get,
        )
        with pytest.raises(DependencyUnavailable, match="HTTP 302") as info:
            post_only.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
        assert not isinstance(info.value, SonderInferenceUnreachable)
        assert stolen == []
    finally:
        close()


def test_a_non_http_peer_is_a_reported_failure_not_a_crash():
    def banner(conn):
        # Consume the request first so the close is orderly (a close with
        # unread input would RST the socket and turn this into a reset).
        _read_request(conn)
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")

    port, close = _serve_raw(banner)
    try:
        config = SonderInferenceConfig(base_url="http://127.0.0.1:%d" % port, health_ttl_seconds=0)
        gateway = SonderInferenceGateway(config)
        status = gateway.provider_status()["sonder_inference"]
        assert status["state"] == "unavailable" and "malformed HTTP" in status["detail"]
        assert gateway.capability_health().healthy is False
        assert gateway.readiness().kind == "unreachable"
        with pytest.raises(DependencyUnavailable):
            gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
        # The send itself (health scripted ready) is a dependency failure,
        # never "unreachable": bytes reached the peer.
        send_only = SonderInferenceGateway(config, get_transport=FakeInference().get)
        with pytest.raises(DependencyUnavailable, match="malformed HTTP") as info:
            send_only.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
        assert not isinstance(info.value, SonderInferenceUnreachable)
    finally:
        close()


def test_health_probe_is_bounded_by_wall_clock_not_per_read():
    import time as _time

    def trickle(conn):
        _read_request(conn)
        for byte in _http_response(200, _health()):
            conn.sendall(bytes([byte]))
            _time.sleep(0.05)

    port, close = _serve_raw(trickle)
    try:
        gateway = SonderInferenceGateway(SonderInferenceConfig(base_url="http://127.0.0.1:%d" % port))
        started = _time.monotonic()
        snapshot = gateway.health(timeout=0.5, refresh=True)
        elapsed = _time.monotonic() - started
        assert snapshot.state == "unavailable" and "timed out" in snapshot.detail
        assert elapsed < 1.5, elapsed

        # The send is bounded by the operation deadline the same way.
        send_only = SonderInferenceGateway(
            SonderInferenceConfig(base_url="http://127.0.0.1:%d" % port),
            get_transport=FakeInference().get,
        )
        started = _time.monotonic()
        with pytest.raises(DeadlineExceeded):
            send_only.generate(ModelRequest(prompt="x", tier="fast"), _ctx(timeout=0.6))
        assert _time.monotonic() - started < 1.6
    finally:
        close()


OVERLOADED = {"error": {"message": "too many connections", "type": "service_unavailable",
                        "code": "overloaded", "param": None}, "sonder": {"api_version": 1}}


def test_overloaded_health_is_transient_capacity_not_an_api_mismatch():
    clock = Clock()
    fake = FakeInference(health=OVERLOADED, health_status=503)
    gateway = _gateway(fake, clock=clock, health_ttl_seconds=5.0)
    snapshot = gateway.health()
    assert snapshot.state == "degraded" and snapshot.overloaded and not snapshot.api_mismatch
    assert "incompatible" not in snapshot.detail
    with pytest.raises(CapacityExceeded) as info:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)
    assert gateway.readiness().kind == "overloaded"
    status = gateway.provider_status()["sonder_inference"]
    assert status["state"] == "degraded" and status["healthy"] is False
    # Load cleared: the very next call re-probes (overload is never cached).
    fake.health, fake.health_status = _health(), 200
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "hello"
    assert fake.posts


def test_real_overloaded_body_is_not_cached_as_a_version_mismatch(loopback_server):
    server, handler = loopback_server
    handler.routes.update({
        "/v1/sonder/health": (503, OVERLOADED),
        "/v1/chat/completions": (200, _chat("after load")),
    })
    gateway = SonderInferenceGateway(SonderInferenceConfig(
        base_url="http://127.0.0.1:%d" % server.server_address[1],
    ))
    with pytest.raises(CapacityExceeded):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    handler.routes["/v1/sonder/health"] = (200, _health())
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "after load"


@pytest.mark.parametrize("document,mismatch", [
    ({"error": {"code": "internal_error"}}, False),
    ({"error": {"code": "x"}, "sonder": {"api_version": 2}}, True),
    ({**_health(), "api_version": 2}, True),
    ({"status": "ready"}, False),
])
def test_version_mismatch_needs_a_reported_version(document, mismatch):
    fake = FakeInference(health=document, health_status=503 if "error" in document else 200)
    snapshot = _gateway(fake).health()
    assert snapshot.api_mismatch is mismatch
    assert snapshot.state != "ready"


def test_concurrent_stale_callers_share_one_health_probe():
    import time as _time

    fake = FakeInference()
    real_get = fake.get

    def slow_get(url, headers, timeout):
        _time.sleep(0.2)
        return real_get(url, headers, timeout)

    gateway = SonderInferenceGateway(
        SonderInferenceConfig(health_ttl_seconds=0), transport=fake.post, get_transport=slow_get,
    )
    barrier = threading.Barrier(8)
    states = []

    def worker():
        barrier.wait()
        states.append(gateway.health().state)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert states == ["ready"] * 8
    assert len(fake.gets) == 1


@pytest.mark.parametrize("code,needle,absent", [
    ("forbidden_host", "127.0.0.1, localhost or [::1]", "SONDER_INFERENCE_API_KEY"),
    ("forbidden_origin", "Origin", "SONDER_INFERENCE_API_KEY"),
    ("unauthorized", "SONDER_INFERENCE_API_KEY", "localhost or"),
])
def test_403_and_401_name_the_actual_problem(code, needle, absent):
    status = 401 if code == "unauthorized" else 403
    fake = FakeInference(chat=lambda url, *a: _http_error(url, status, code))
    with pytest.raises(Forbidden) as info:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert needle in str(info.value) and absent not in str(info.value)
    assert code in str(info.value)

    health = FakeInference(health={"error": {"code": code, "message": "no"}}, health_status=status)
    snapshot = _gateway(health).health()
    assert code in snapshot.detail and needle in snapshot.detail
    assert snapshot.host_rejected is (code != "unauthorized")
    with pytest.raises(Forbidden, match=code):
        _gateway(health).generate(ModelRequest(prompt="x", tier="fast"), _ctx())


def test_down_server_message_is_stated_once():
    gateway = SonderInferenceGateway(SonderInferenceConfig(
        base_url="http://127.0.0.1:%d" % _refused_port(),
    ))
    with pytest.raises(SonderInferenceUnreachable) as info:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    message = str(info.value)
    assert message.count("not reachable") == 1 and message.count("sonder-infer serve") == 1
    assert "restart" in message
    assert info.value.reason == "connection refused"
    assert gateway.health().detail == "connection refused"
    assert gateway.provider_status()["sonder_inference"]["detail"] == "connection refused"


def test_inference_metrics_have_their_own_bounded_backend_label():
    from sonder_runtime.adapters.inference.telemetry import from_openai_compatible
    from sonder_runtime.platform.metrics import MetricsRegistry

    class Recorder:
        def __init__(self):
            self.labels_seen = []

        def labels(self, **labels):
            self.labels_seen.append(labels["backend"])
            return self

        def observe(self, _value):
            return None

        inc = observe

    registry = MetricsRegistry(enabled=False)
    recorder = Recorder()
    for name in ("model_backend_phase_duration_seconds", "model_token_throughput_per_second",
                 "model_prompt_tokens", "model_load_states_total"):
        setattr(registry, name, recorder)
    registry.observe_inference("sonder_inference", from_openai_compatible(_chat()))
    registry.observe_inference("unbounded-name", from_openai_compatible(_chat()))
    assert recorder.labels_seen
    assert set(recorder.labels_seen) == {"sonder_inference", "other"}


# A3: trial3's busy proxy must not look like a dead provider.
@pytest.mark.parametrize("failure", ["timeout", "overloaded"])
def test_a3_recent_healthy_cache_survives_busy_probe_without_renewing_age(failure):
    clock, fake = Clock(), FakeInference()
    gateway = _gateway(fake, clock=clock)
    healthy = gateway.health()
    clock.now += 6
    if failure == "timeout":
        fake.get_error = TimeoutError("busy")
    else:
        fake.health, fake.health_status = OVERLOADED, 503
    busy = gateway.health()
    assert busy.state == "ready" and busy.busy
    assert busy.checked_monotonic == healthy.checked_monotonic
    assert busy.document == healthy.document
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "hello"
    assert "busy" in gateway.readiness().detail
    assert "busy" in gateway.capability_health().detail
    status = gateway.provider_status()["sonder_inference"]
    assert status["busy"] is True and status["healthy"] is True
    clock.now = healthy.checked_monotonic + 121
    with pytest.raises((DependencyUnavailable, CapacityExceeded)):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())


def test_a3_six_second_health_server_allows_call_with_recent_healthy_cache():
    import time
    from sonder_runtime.adapters.inference.sonder_inference_gateway import direct_get_transport

    finished = threading.Event()

    def handler(conn):
        request = _read_request(conn)
        if b"/v1/sonder/health" in request:
            try:
                time.sleep(6)
                conn.sendall(_http_response(200, _health()))
            finally:
                finished.set()
        else:
            conn.sendall(_http_response(200, _chat("proceeded")))

    port, close = _serve_raw(handler)
    try:
        gateway = SonderInferenceGateway(
            SonderInferenceConfig(base_url="http://127.0.0.1:%d" % port,
                                  health_ttl_seconds=0),
            get_transport=FakeInference().get,
        )
        gateway.health()
        gateway._get_transport = direct_get_transport
        response = gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
        assert response.text == "proceeded"
    finally:
        close()
        assert finished.wait(7), "fake health handler must finish"


def test_a3_stale_health_timeout_is_unavailable_and_retried_once():
    clock, fake = Clock(), FakeInference()
    gateway = _gateway(fake, clock=clock, base_url="http://127.0.0.1:18888")
    gateway.health()
    clock.now += 121
    fake.get_error = TimeoutError("busy")
    with pytest.raises(SonderInferenceUnreachable) as caught:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert caught.value.kind == "provider_unavailable"
    assert "http://127.0.0.1:18888" in str(caught.value)
    assert [item[2] for item in fake.gets[1:]] == [5.0, 6.0]
    assert fake.posts == []


def test_a3_health_retry_can_recover_without_a_cache():
    fake = FakeInference()
    gateway = _gateway(fake)
    attempts = []

    def get(url, headers, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise TimeoutError("busy")
        return fake.get(url, headers, timeout)

    gateway._get_transport = get
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "hello"
    assert attempts == [5.0, 6.0]


def test_a3_health_timeout_and_stale_window_are_configurable():
    cfg = config_from_env({"SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS": "1.5",
                           "SONDER_INFERENCE_HEALTH_STALE_SECONDS": "12"})
    assert cfg.health_timeout_seconds == 1.5
    assert cfg.health_stale_seconds == 12
    fake = FakeInference()
    fake.get_error = TimeoutError()
    with pytest.raises(SonderInferenceUnreachable):
        _gateway(fake, cfg).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert [item[2] for item in fake.gets] == [1.5, 3.0]


class _RetryToken:
    cancelled = False

    def __init__(self):
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.cancelled


@pytest.mark.parametrize("retry_after,delay", [(None, 5), ("2", 2), ("999999", 5),
                                               ("invalid", 5), ("-1", 5)])
def test_a3_backend_read_timeout_retries_once_then_is_busy_timeout(retry_after, delay):
    def chat(url, *_):
        error = _http_error(url, 503, "backend_unavailable", "backend read timed out")
        if retry_after is not None:
            error.headers["Retry-After"] = retry_after
        return error

    fake, token = FakeInference(chat=chat), _RetryToken()
    with pytest.raises(DependencyUnavailable) as caught:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=token))
    assert getattr(caught.value, "kind", None) == "busy_timeout"
    assert not isinstance(caught.value, SonderInferenceUnreachable)
    assert len(fake.posts) == 2
    assert token.waits == [delay]


def test_a3_backend_read_timeout_retry_can_succeed():
    fake, token = FakeInference(), _RetryToken()
    fake.chat = lambda url, *_: (
        _http_error(url, 503, "backend_unavailable", "read timed out")
        if len(fake.posts) == 1 else _chat("recovered")
    )
    response = _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"),
                                       _ctx(cancellation=token))
    assert response.text == "recovered"
    assert len(fake.posts) == 2 and token.waits == [5]


def test_a3_partial_stream_is_never_replayed():
    from sonder_runtime.application.chat import stream_sink

    live = stream_sink.LiveTurnStream(lambda text: True)
    fake, token = FakeInference(), _RetryToken()

    def chat(url, *_):
        live.emit("partial")
        return _http_error(url, 503, "backend_unavailable", "read timed out")

    fake.chat = chat
    with stream_sink.armed(live), stream_sink.claimed_for_call(), pytest.raises(DependencyUnavailable):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=token))
    assert len(fake.posts) == 1 and token.waits == []


@pytest.mark.parametrize("source,session,priority,agent", [
    ("http", "chat-1", "interactive", None),
    ("repl", "chat-1", "interactive", None),
    ("worker", "agent-run-1", "subagent", "agent-run-1"),
    ("system", "maintenance-1", "background", None),
])
def test_a3_cache_key_is_stable_per_run_and_headers_describe_work(source, session, priority, agent):
    from sonder_runtime.application.context import bind_operation_context

    fake, gateway = FakeInference(), None
    gateway = _gateway(fake)
    for run in (session, session + "-next"):
        ambient = local_owner_context(correlation_id="outer", source=source, session_id=run)
        with bind_operation_context(ambient):
            for turn in range(3):
                gateway.generate(ModelRequest(prompt="x", tier="fast"),
                                 _ctx(correlation="turn-%d" % turn, source=source))
    keys = [p[1].get("prompt_cache_key") for p in fake.posts]
    assert keys[:3] == [session] * 3
    assert keys[3:] == [session + "-next"] * 3
    assert fake.posts[0][2]["X-Sonder-Priority"] == priority
    assert fake.posts[0][2].get("X-Sonder-Agent-Id") == agent
    assert fake.posts[0][2]["X-Sonder-Run-Id"] == "turn-0"


def test_a3_missing_session_does_not_use_per_call_correlation_as_cache_key():
    fake = FakeInference()
    _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert "prompt_cache_key" not in fake.posts[0][1]
    assert "X-Sonder-Agent-Id" not in fake.posts[0][2]


def test_a3_reasoning_budget_and_sampling_reach_request_body():
    health = _health()
    health["sonder"] = {"features": ["thinking", "reasoning_budget"]}
    fake = FakeInference(health=health)
    options = {"think": False, "reasoning_budget_tokens": 256,
               "reasoning_budget_message": "Answer now", "top_p": 0.95,
               "top_k": 20, "min_p": 0.0, "repeat_penalty": 1.1, "seed": 42}
    _gateway(fake).generate(ModelRequest(prompt="x", tier="fast", options=options), _ctx())
    body = fake.posts[0][1]
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    for key, value in options.items():
        if key != "think":
            assert body[key] == value


def test_empty_length_without_explicit_thinking_retries_once_with_thinking_off():
    first = _chat("")
    first["choices"][0]["finish_reason"] = "length"
    responses = iter((first, _chat("OK")))
    fake = FakeInference(chat=lambda *_: next(responses))
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(), transport=fake.post, get_transport=fake.get,
        env={"SONDER_INFERENCE_THINKING": "on"},
    )

    response = gateway.generate(ModelRequest(prompt="Reply OK", tier="general"), _ctx())

    assert response.text == "OK"
    assert len(fake.posts) == 2
    assert fake.posts[0][1]["messages"] == fake.posts[1][1]["messages"]
    assert fake.posts[1][1]["chat_template_kwargs"]["enable_thinking"] is False
    assert fake.posts[1][1]["stream"] is False
    assert gateway.last_response_meta["empty_length_recovered"] is True


@pytest.mark.parametrize("think", [True, False])
def test_empty_length_with_explicit_thinking_choice_is_not_replayed(think):
    first = _chat("")
    first["choices"][0]["finish_reason"] = "length"
    fake = FakeInference(chat=lambda *_: first)
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(), transport=fake.post, get_transport=fake.get,
        env={"SONDER_INFERENCE_THINKING": "on"},
    )

    with pytest.raises(ModelCallError, match="done_reason"):
        gateway.generate(ModelRequest(prompt="Reply OK", tier="general",
                                      options={"think": think}), _ctx())

    assert len(fake.posts) == 1


def test_empty_length_repair_is_bounded_when_second_response_is_empty():
    empty = _chat("")
    empty["choices"][0]["finish_reason"] = "length"
    fake = FakeInference(chat=lambda *_: empty)
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(), transport=fake.post, get_transport=fake.get,
        env={"SONDER_INFERENCE_THINKING": "on"},
    )

    with pytest.raises(ModelCallError, match="done_reason"):
        gateway.generate(ModelRequest(prompt="Reply OK", tier="general"), _ctx())

    assert len(fake.posts) == 2


@pytest.mark.parametrize("location", ["nested", "cached_tokens", "missing"])
def test_a3_timings_finish_reason_and_activity_are_reported(location, monkeypatch):
    from sonder_runtime.adapters.observability import activity_tracker
    from sonder_runtime.application.ports.model_gateway import ModelResponse

    data = _chat()
    data.pop("timings")
    expected = {"cache_n": 3, "prompt_n": 4, "predicted_n": 6,
                "queue_ms": 12.5, "draft_n": 8, "draft_n_accepted": 5}
    if location == "nested":
        data["usage"]["sonder"] = {"timings": {**expected, "secret": "never copy"}}
    elif location == "cached_tokens":
        data["usage"]["prompt_tokens_details"] = {"cached_tokens": 0}
        expected = {"cache_n": 0}
    else:
        expected = None
    data["choices"][0]["finish_reason"] = "length"
    events = []
    monkeypatch.setattr(activity_tracker, "record_event", lambda kind, **fields: events.append((kind, fields)))
    gateway = _gateway(FakeInference(chat=lambda *_: data))
    response = gateway.generate(ModelRequest(prompt="private prompt", tier="fast"), _ctx())
    assert isinstance(response, ModelResponse)
    assert response.timings == expected
    assert response.finish_reason == "length"
    assert gateway.last_response_meta["finish_reason"] == "length"
    assert gateway.last_response_meta.get("timings") == expected
    assert events[-1][1]["finish_reason"] == "length"
    assert events[-1][1].get("timings") == expected
    assert "private prompt" not in repr(events) and "never copy" not in repr(events)


@pytest.mark.parametrize("correlation", ["standalone-012345", "repl-work-012345"])
def test_a3_explicit_agent_run_correlations_are_stable_keys(correlation):
    fake = FakeInference()
    gateway = _gateway(fake)
    for _ in range(3):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(correlation=correlation, source="repl"))
    assert [p[1].get("prompt_cache_key") for p in fake.posts] == [correlation] * 3
    assert fake.posts[0][2]["X-Sonder-Agent-Id"] == correlation
    assert fake.posts[0][2]["X-Sonder-Priority"] == "subagent"


def test_a3_read_timeout_beyond_display_limit_and_lowercase_retry_header():
    fake, token = FakeInference(), _RetryToken()

    def chat(url, *_):
        error = _http_error(url, 503, "backend_unavailable", "x" * 250 + " read timed out")
        error.headers["retry-after"] = "0"
        return error

    fake.chat = chat
    with pytest.raises(DependencyUnavailable) as caught:
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=token))
    assert getattr(caught.value, "kind", None) == "busy_timeout"
    assert len(fake.posts) == 2 and token.waits == [0]


@pytest.mark.parametrize("value", [True, -1, "2", float("nan"), float("inf")])
def test_a3_invalid_optional_timings_do_not_become_measurements(value):
    data = _chat()
    data["timings"] = {}
    data["usage"]["sonder"] = {"timings": {key: value for key in
        ("cache_n", "prompt_n", "predicted_n", "queue_ms", "draft_n", "draft_n_accepted")}}
    response = _gateway(FakeInference(chat=lambda *_: data)).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert response.timings is None


@pytest.mark.parametrize("failure", ["auth", "version", "starting", "refused"])
def test_a3_good_cache_never_masks_definitive_health_failure(failure):
    clock, fake = Clock(), FakeInference()
    gateway = _gateway(fake, clock=clock)
    gateway.health()
    clock.now += 6
    if failure == "auth":
        fake.health, fake.health_status = {"error": {"code": "unauthorized"}}, 401
    elif failure == "version":
        fake.health = _health(api_version=2)
    elif failure == "starting":
        fake.health, fake.health_status = _health("starting"), 503
    else:
        fake.get_error = ConnectionRefusedError()
    with pytest.raises((Forbidden, DependencyUnavailable)):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert fake.posts == []
    assert len(fake.gets) == 2


def test_a3_busy_retry_obeys_cancellation_and_short_deadline():
    from sonder_runtime.domain.common.errors import Cancelled

    class CancelDuringWait(_RetryToken):
        def wait(self, timeout):
            super().wait(timeout)
            self.cancelled = True
            return True

    for token, timeout, expected in [(_RetryToken(), 0.1, DependencyUnavailable),
                                     (CancelDuringWait(), 30, Cancelled)]:
        fake = FakeInference(chat=lambda url, *_: _http_error(url, 503, "backend_unavailable", "read timed out"))
        with pytest.raises(expected):
            _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"),
                                    _ctx(cancellation=token, timeout=timeout))
        assert len(fake.posts) == 1


def test_a3_response_meta_does_not_leak_previous_success():
    fake = FakeInference()
    gateway = _gateway(fake)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert gateway.last_response_meta["finish_reason"] == "stop"
    fake.chat = lambda *_: TimeoutError()
    with pytest.raises(DeadlineExceeded):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert gateway.last_response_meta == {}


def test_a3_ambient_session_never_crosses_principal_boundary():
    from dataclasses import replace
    from sonder_runtime.application.context import bind_operation_context

    ambient = replace(_ctx(source="worker"), session_id="another-principal", principal_id="other")
    fake = FakeInference()
    with bind_operation_context(ambient):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert "prompt_cache_key" not in fake.posts[0][1]
    assert "X-Sonder-Agent-Id" not in fake.posts[0][2]


@pytest.mark.parametrize("name,value", [
    ("SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS", "0"),
    ("SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS", "nan"),
    ("SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS", "inf"),
    ("SONDER_INFERENCE_HEALTH_STALE_SECONDS", "-1"),
    ("SONDER_INFERENCE_HEALTH_STALE_SECONDS", "inf"),
])
def test_a3_invalid_health_configuration_fails_closed(name, value):
    with pytest.raises(InvalidInput):
        config_from_env({name: value})


def test_a3_busy_status_does_not_add_an_identity_probe():
    clock, fake = Clock(), FakeInference()
    gateway = _gateway(fake, clock=clock)
    gateway.health()
    clock.now += 6
    fake.get_error = TimeoutError("busy")
    status = gateway.provider_status()["sonder_inference"]
    assert status["busy"] is True
    assert len(fake.gets) == 2
    assert all("/identity" not in item[0] for item in fake.gets)


def test_a3_doctor_and_preflight_surface_busy(monkeypatch):
    import sonder_doctor
    from sonder_runtime.adapters import preflight
    from sonder_runtime.adapters.inference import sonder_inference_gateway as module

    clock, fake = Clock(), FakeInference()
    gateway = _gateway(fake, clock=clock)
    gateway.health()
    clock.now += 6
    fake.get_error = TimeoutError("busy")
    env = {"SONDER_MODEL_BACKEND": "sonder-inference", "SONDER_EMBEDDING_PROVIDER": "ollama"}
    doctor = sonder_doctor._check_sonder_inference(env=env, gateway=gateway)
    assert doctor["status"] == "ok" and "busy" in doctor["detail"]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "SonderInferenceGateway", lambda: gateway)
    check = preflight._sonder_inference_result()
    assert check.ok and "busy" in check.detail


def test_a3_busy_timeout_never_falls_back_to_ollama():
    from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway

    class Tripwire:
        def generate(self, *_):
            pytest.fail("busy inference must never send a request to Ollama/127.0.0.1:11434")

    fake = FakeInference(chat=lambda url, *_: _http_error(url, 503, "backend_unavailable", "read timed out"))
    gateway = PreSendFallbackGateway(_gateway(fake), fallback=Tripwire())
    with pytest.raises(DependencyUnavailable, match="busy_timeout"):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=_RetryToken()))
    assert len(fake.posts) == 2


def test_a3_activity_detailed_feed_preserves_content_free_measurements():
    from sonder_runtime.adapters.observability import activity_tracker

    with activity_tracker.response_span("a3-test") as span:
        _gateway(FakeInference()).generate(ModelRequest(prompt="never publish this", tier="fast"), _ctx())
        public = activity_tracker.public_response(span, include_detail=True)
        event = next(e for e in public["events"] if e["kind"] == "inference_outcome")
        summary = json.loads(event["summary"])
        assert summary["finish_reason"] == "stop"
        assert summary["timings"] == {"prompt_n": 4, "predicted_n": 6}
        assert "never publish this" not in repr(public)
