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
    STATUS_KEYS,
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
    config_from_env,
    correlation_headers,
)
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
    {"format": "json"}, {"tools": [{"type": "function"}]}, {"think": True},
    {"response_format": {"type": "json_object"}}, {"temperature": "hot"},
    {"stop": ["a", "b", "c", "d", "e"]}, {"num_predict": 1.5}, {"top_k": True},
])
def test_unsupported_or_invalid_options_are_refused_before_any_send(options):
    fake = FakeInference()
    with pytest.raises(InvalidInput):
        _gateway(fake).generate(ModelRequest(prompt="x", tier="fast", options=options), _ctx())
    assert fake.posts == [] and fake.gets == []


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


def test_health_probe_timeout_is_bounded_to_two_seconds():
    fake = FakeInference()
    gateway = _gateway(fake, timeout_seconds=300.0)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(timeout=120.0))
    assert fake.gets[0][2] <= 2.0
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


@pytest.mark.parametrize("url", ["http://127.0.0.1:1", "http://localhost:2", "http://[::1]:3",
                                 "http://127.1.2.3:4"])
def test_loopback_needs_no_consent(url):
    fake = FakeInference()
    _gateway(fake, base_url=url).generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert len(fake.posts) == 1


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
    gateway = SonderInferenceGateway(SonderInferenceConfig(base_url=base, health_ttl_seconds=0))
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
    assert gateway.provider_status()["sonder_inference"]["state"] == "degraded"


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
