"""OpenRouterGateway against an in-process fake OpenRouter HTTP server.

No test here touches the network or a real key: the fake server listens on
loopback (``SONDER_OPENROUTER_BASE_URL`` is the only override of the fixed
https endpoint, and plain http is accepted for loopback only), and the key is
a synthetic ``sk-or-v1-...`` value whose absence from every log line, error,
status document and metrics export is asserted.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference.openai_protocol_probe import OpenAICompatibleProtocolProbe
from sonder_runtime.adapters.inference.openrouter_gateway import (
    DEFAULT_BASE_URL,
    OpenRouterCreditsExhausted,
    OpenRouterGateway,
    OpenRouterRateLimited,
    OpenRouterSettings,
    config_from_env,
    normalize_base_url,
    protocol_probe_gateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.session import provider_attempts
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from sonder_runtime.domain.openrouter_policy import (
    SAFE_PROVIDER_DEFAULTS,
    OpenRouterPolicyError,
    model_summary,
    normalize_provider_preferences,
    validate_model_id,
)
from sonder_runtime.domain.routing.backend_conformance import BackendIdentity
from sonder_runtime.domain.security.redaction import redact_text
from sonder_runtime.platform.metrics import MetricsRegistry
from tests.test_openrouter_stream_backpressure import observed as observed
from tests.test_openrouter_stream_terminal import Observer, _capture

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
MODEL = "anthropic/claude-sonnet-4"


def _chat(text="hello from the router", *, model=MODEL, provider="DeepInfra"):
    return {
        "id": "gen-1", "object": "chat.completion", "created": 1, "model": model,
        "provider": provider,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                  "cost": 0.00042,
                  "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 0},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }


CATALOG = [
    {"id": "anthropic/claude-sonnet-4", "name": "Anthropic: Claude Sonnet 4",
     "context_length": 200000,
     "pricing": {"prompt": "0.000003", "completion": "0.000015"},
     "supported_parameters": ["tools", "tool_choice", "response_format", "structured_outputs"]},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "name": "Meta: Llama 3.3 70B (free)",
     "context_length": 65536, "pricing": {"prompt": "0", "completion": "0"},
     "supported_parameters": ["temperature"]},
    {"id": "openrouter/auto", "name": "Auto Router", "context_length": 2000000,
     "pricing": {"prompt": "-1", "completion": "-1"}, "supported_parameters": ["tools"]},
]


class FakeOpenRouter:
    """Scriptable responses; every request is recorded (path, headers, body)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.chat_status = 200
        self.chat_body: object = _chat()
        self.chat_headers: dict[str, str] = {}
        self.stream_events: list[str] | None = None
        self.user_models_status = 200
        self.credits_status = 200
        self.lock = threading.Lock()

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        with self.lock:
            self.requests.append({"method": method, "path": handler.path,
                                  "headers": dict(handler.headers), "body": body})
        path = handler.path
        if method == "POST" and path == "/api/v1/chat/completions":
            if body.get("stream") and self.stream_events is not None and self.chat_status == 200:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.end_headers()
                for event in self.stream_events:
                    handler.wfile.write(event.encode() + b"\n\n")
                    handler.wfile.flush()
                return
            return self._json(handler, self.chat_status, self.chat_body, self.chat_headers)
        if path == "/api/v1/models/user":
            if self.user_models_status != 200:
                return self._json(handler, self.user_models_status,
                                  {"error": {"code": self.user_models_status, "message": "nope"}})
            return self._json(handler, 200, {"data": CATALOG[:2], "total_count": 2})
        if path == "/api/v1/models":
            return self._json(handler, 200, {"data": CATALOG})
        if path == "/api/v1/key":
            return self._json(handler, 200, {"data": {
                "label": "sk-or-v1-abc...xyz", "limit": 25.0, "limit_remaining": 20.5,
                "usage": 4.5, "usage_daily": 0.25, "usage_weekly": 1.0, "usage_monthly": 4.5,
                "is_free_tier": False,
            }})
        if path == "/api/v1/credits":
            if self.credits_status != 200:
                return self._json(handler, self.credits_status, {"error": {
                    "code": self.credits_status, "message": "Only management keys can perform this operation"}})
            return self._json(handler, 200, {"data": {"total_credits": 50.0, "total_usage": 29.5}})
        return self._json(handler, 404, {"error": {"code": 404, "message": "not found"}})

    @staticmethod
    def _json(handler, status, document, headers=None):
        data = json.dumps(document).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(data)

    def chat_requests(self):
        return [item for item in self.requests if item["path"].endswith("/chat/completions")]


@pytest.fixture
def fake():
    state = FakeOpenRouter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state.handle(self, "GET")

        def do_POST(self):  # noqa: N802
            state.handle(self, "POST")

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


def _env(fake, **extra):
    env = {
        "SONDER_ALLOW_CLOUD": "1",
        "OPENROUTER_API_KEY": FAKE_KEY,
        "SONDER_OPENROUTER_BASE_URL": fake.base_url,
        "SONDER_OPENROUTER_MODEL": MODEL,
    }
    env.update(extra)
    return env


def _gateway(fake, policy=None, **extra):
    return OpenRouterGateway(env=_env(fake, **extra), policy_models=lambda: dict(policy or {}))


def _context(cloud=True):
    return local_owner_context(correlation_id="t", cloud_allowed=cloud, timeout_seconds=30)


def _request(tier="code", prompt="write a function", **options):
    return ModelRequest(prompt=prompt, tier=tier, system="be terse", options=options)


# -- chat, provider object, accounting ---------------------------------------


def test_chat_sends_safe_provider_defaults_and_records_usage(fake):
    gateway = _gateway(fake)
    response = gateway.generate(_request(temperature=0.1, num_predict=64, num_ctx=8192), _context())
    assert response.text == "hello from the router"
    assert response.model == MODEL
    assert (response.tokens_in, response.tokens_out) == (12, 5)
    assert response.telemetry.prompt_cached_tokens == 4
    assert response.telemetry.prompt_uncached_tokens == 8
    (sent,) = fake.chat_requests()
    body = sent["body"]
    assert body["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}
    assert body["model"] == MODEL and body["stream"] is False
    assert body["max_tokens"] == 64 and body["temperature"] == 0.1
    assert "num_ctx" not in body  # a local-only hint is dropped
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert sent["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    # Attribution headers are opt-in.
    assert "HTTP-Referer" not in sent["headers"] and "X-Title" not in sent["headers"]
    usage = gateway.last_usage
    assert usage["cost_usd"] == pytest.approx(0.00042)
    assert usage["upstream_provider"] == "DeepInfra"
    assert usage["cached_tokens"] == 4


def test_provider_preferences_layer_default_order_and_tier(fake):
    gateway = _gateway(
        fake,
        SONDER_OPENROUTER_PROVIDER=json.dumps({"sort": "throughput", "require_parameters": True}),
        SONDER_OPENROUTER_PROVIDER_ORDER="DeepInfra, fireworks",
        SONDER_OPENROUTER_TIER_PROVIDER=json.dumps({"code": {"zdr": False, "only": ["fireworks"]}}),
        SONDER_OPENROUTER_APP_TITLE="Sonder Runtime",
    )
    gateway.generate(_request(tier="code"), _context())
    gateway.generate(_request(tier="fast"), _context())
    code, fast = (item["body"]["provider"] for item in fake.chat_requests())
    assert code == {"data_collection": "deny", "zdr": False, "allow_fallbacks": True,
                    "sort": "throughput", "require_parameters": True,
                    "order": ["deepinfra", "fireworks"], "only": ["fireworks"]}
    assert fast["zdr"] is True and fast["order"] == ["deepinfra", "fireworks"]
    assert fake.chat_requests()[0]["headers"]["X-Title"] == "Sonder Runtime"


def test_provider_preferences_reject_unknown_keys_and_values():
    with pytest.raises(OpenRouterPolicyError, match="unknown key"):
        normalize_provider_preferences({"data_colection": "deny"})
    with pytest.raises(OpenRouterPolicyError, match="allow"):
        normalize_provider_preferences({"data_collection": "maybe"})
    with pytest.raises(OpenRouterPolicyError, match="true or false"):
        normalize_provider_preferences({"zdr": "yes"})
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_OPENROUTER_PROVIDER": "{not json"}, policy_models=None)
    assert dict(SAFE_PROVIDER_DEFAULTS) == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}


def test_streaming_yields_deltas_then_final_usage(fake, monkeypatch):
    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    fake.stream_events = [
        ": OPENROUTER PROCESSING",
        'data: {"id":"g","model":"%s","provider":"Fireworks","choices":[{"delta":{"content":"Hel"}}]}' % MODEL,
        'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"cost":0.0001}}',
        "data: [DONE]",
    ]
    gateway = _gateway(fake)
    chunks = list(gateway.stream(_request(), _context()))
    assert [chunk.text for chunk in chunks[:-1]] == ["Hel", "lo"]
    final = chunks[-1]
    assert (final.finish_reason, final.input_tokens, final.output_tokens) == ("stop", 3, 2)
    assert fake.chat_requests()[0]["body"]["stream"] is True
    assert fake.chat_requests()[0]["body"]["provider"]["zdr"] is True
    assert gateway.last_usage["upstream_provider"] == "Fireworks"
    assert len(usage) == 1
    assert usage[0]["cost_usd"] == pytest.approx(0.0001)


def test_returned_accounting_redacts_labels_and_rejects_invalid_counts(fake, monkeypatch, caplog):
    from sonder_runtime.adapters.inference.openrouter_gateway import DETAIL_LIMIT

    key = "synthetic-accounting-secret-" + "k" * 300
    gateway = _gateway(fake, OPENROUTER_API_KEY=key)
    data = _chat(model=key, provider="prefix-" + key)
    data["usage"] = {"cost": float("nan"), "prompt_tokens": True,
                     "completion_tokens": 1_000_000_001,
                     "prompt_tokens_details": {"cached_tokens": -1}}
    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    gateway._raw_post = lambda *_args: data
    with pytest.raises(SonderError):
        gateway.generate(_request(), _context())
    assert len(usage) == 1
    facts = usage[0]
    assert facts["model"] == "[REDACTED]"
    assert facts["upstream_provider"] == "prefix-[REDACTED]"
    assert all(facts[field] is None for field in ("cost_usd", "prompt_tokens", "completion_tokens", "cached_tokens"))
    assert key not in json.dumps(dict(gateway.last_usage)) + caplog.text
    assert all(len(facts[field]) <= DETAIL_LIMIT for field in ("model", "upstream_provider"))


def test_completed_response_accounts_once_when_dispatch_capture_fails(fake, monkeypatch):
    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)

    def dispatch(_provider, _path, _payload, send):
        send()
        raise DependencyUnavailable("synthetic post-response evidence failure")

    monkeypatch.setattr("sonder_runtime.adapters.inference.openai_compat_gateway.dispatch_provider", dispatch)
    gateway = _gateway(fake)
    with pytest.raises(DependencyUnavailable, match="evidence failure"):
        gateway.generate(_request(), _context())
    assert len(fake.chat_requests()) == len(usage) == 1
    assert usage[0]["cost_usd"] == pytest.approx(0.00042)
    assert gateway.last_usage == usage[0]


def test_stream_error_maps_like_generate(fake):
    fake.stream_events = None
    fake.chat_status, fake.chat_body = 402, {"error": {"code": 402, "message": "Insufficient credits"}}
    with pytest.raises(OpenRouterCreditsExhausted):
        list(_gateway(fake).stream(_request(), _context()))


# -- model selection -----------------------------------------------------------


def test_model_precedence_explicit_env_policy_default(fake):
    policy = {"code": "qwen/qwen3-coder", "fast": "google/gemini-2.5-flash"}
    gateway = _gateway(fake, policy=policy, SONDER_OPENROUTER_TIER_MODELS="code=openai/gpt-5-mini")
    gateway.generate(_request(tier="code"), _context())
    gateway.generate(_request(tier="fast"), _context())
    gateway.generate(_request(tier="general"), _context())
    gateway.generate(_request(tier="code", model="mistralai/devstral-small:free"), _context())
    assert [item["body"]["model"] for item in fake.chat_requests()] == [
        "openai/gpt-5-mini", "google/gemini-2.5-flash", MODEL, "mistralai/devstral-small:free",
    ]
    status = gateway.provider_status()["openrouter"]
    assert status["tier_models"]["code"] == "openai/gpt-5-mini"
    assert gateway.served_tier_models()["openrouter"]["fast"] == "google/gemini-2.5-flash"


def test_model_ids_are_validated_before_any_send(fake):
    with pytest.raises(InvalidInput, match="vendor/model"):
        _gateway(fake).generate(_request(model="not a model"), _context())
    with pytest.raises(InvalidInput, match="vendor/model"):
        config_from_env(_env(fake, SONDER_OPENROUTER_TIER_MODELS="code=gpt4"), policy_models=None)
    gateway = OpenRouterGateway(env=_env(fake, SONDER_OPENROUTER_MODEL=""), policy_models=None)
    with pytest.raises(InvalidInput, match="openrouter use code"):
        gateway.generate(_request(tier="code"), _context())
    assert fake.chat_requests() == []
    for good in ("openai/gpt-4o", "meta-llama/llama-3.3-70b-instruct:free", "~anthropic/claude-sonnet-latest"):
        assert validate_model_id(good) == good
    for bad in ("gpt-4o", "a/b/c", "../etc", "vendor/model?x=1", "vendor/ model"):
        with pytest.raises(OpenRouterPolicyError):
            validate_model_id(bad)


def test_base_url_is_fixed_https_unless_loopback():
    assert config_from_env({}, policy_models=None).base_url == DEFAULT_BASE_URL
    with pytest.raises(InvalidInput, match="https"):
        normalize_base_url("http://openrouter.example/api/v1")
    with pytest.raises(InvalidInput, match="/v1"):
        normalize_base_url("https://openrouter.ai/api")
    assert normalize_base_url("http://127.0.0.1:9/api/v1/") == "http://127.0.0.1:9/api/v1"


# -- consent -------------------------------------------------------------------


def test_cloud_off_refuses_everything_before_any_request(fake):
    gateway = _gateway(fake, SONDER_ALLOW_CLOUD="0")
    with pytest.raises(Forbidden, match="SONDER_ALLOW_CLOUD"):
        gateway.generate(_request(), _context())
    with pytest.raises(Forbidden, match="SONDER_ALLOW_CLOUD"):
        gateway.list_models()
    with pytest.raises(Forbidden, match="SONDER_ALLOW_CLOUD"):
        gateway.account()
    with pytest.raises(Forbidden):
        list(gateway.stream(_request(), _context()))
    assert fake.requests == []
    assert gateway.capability_health().healthy is False


def test_context_without_cloud_is_refused_even_with_opt_in(fake):
    with pytest.raises(Forbidden, match="operation context"):
        _gateway(fake).generate(_request(), _context(cloud=False))
    assert fake.requests == []


def test_missing_key_names_the_variable(fake):
    gateway = _gateway(fake, OPENROUTER_API_KEY="")
    with pytest.raises(InvalidInput, match="OPENROUTER_API_KEY"):
        gateway.generate(_request(), _context())
    with pytest.raises(InvalidInput, match="OPENROUTER_API_KEY"):
        gateway.account()
    assert fake.requests == []


def test_consent_applies_to_default_endpoint_config_by_default():
    settings = config_from_env({}, policy_models=None)
    assert settings.cloud_enabled is False
    assert settings.api_key == ""


# -- errors --------------------------------------------------------------------


@pytest.mark.parametrize("status,error,needle", [
    (401, Forbidden, "OPENROUTER_API_KEY"),
    (402, OpenRouterCreditsExhausted, "insufficient OpenRouter credits"),
    (403, Forbidden, "moderation"),
    (404, InvalidInput, "provider preferences"),
    (502, DependencyUnavailable, "HTTP 502"),
    (503, DependencyUnavailable, "provider preferences"),
])
def test_http_errors_map_to_domain_errors(fake, status, error, needle):
    fake.chat_status = status
    fake.chat_body = {"error": {"code": status, "message": "upstream said no"}}
    with pytest.raises(error, match=needle):
        _gateway(fake).generate(_request(), _context())


def test_429_honours_retry_after_as_a_cooldown(fake):
    fake.chat_status, fake.chat_headers = 429, {"Retry-After": "30"}
    fake.chat_body = {"error": {"code": 429, "message": "Rate limit exceeded"}}
    now = [1000.0]
    gateway = OpenRouterGateway(env=_env(fake), policy_models=None, monotonic=lambda: now[0])
    with pytest.raises(OpenRouterRateLimited, match="retry after 30s") as first:
        gateway.generate(_request(), _context())
    assert first.value.retry_after == 30.0
    assert isinstance(first.value, CapacityExceeded)
    fake.chat_status, fake.chat_headers, fake.chat_body = 200, {}, _chat()
    with pytest.raises(OpenRouterRateLimited, match="not sending"):
        gateway.generate(_request(), _context())
    assert len(fake.chat_requests()) == 1  # the cool-down refused before sending
    now[0] += 31
    assert gateway.generate(_request(), _context()).text


# -- discovery -----------------------------------------------------------------


def test_model_listing_uses_account_filter_and_converts_prices(fake):
    listing = _gateway(fake).list_models()
    assert listing["source"] == "account" and listing["total"] == 2
    sonnet = listing["models"][0]
    assert sonnet == {
        "id": "anthropic/claude-sonnet-4", "name": "Anthropic: Claude Sonnet 4",
        "context_length": 200000, "prompt_usd_per_million": 3.0,
        "completion_usd_per_million": 15.0, "supports_tools": True,
        "supports_structured_outputs": True,
    }
    user_request = [item for item in fake.requests if item["path"] == "/api/v1/models/user"][0]
    assert user_request["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    assert _gateway(fake).list_models(tools=True)["count"] == 1
    assert _gateway(fake).list_models(search="LLAMA")["models"][0]["id"].startswith("meta-llama/")


def test_model_listing_falls_back_to_public_catalog(fake):
    fake.user_models_status = 403
    listing = _gateway(fake).list_models()
    assert listing["source"] == "public" and listing["total"] == 3
    auto = [row for row in listing["models"] if row["id"] == "openrouter/auto"][0]
    assert auto["prompt_usd_per_million"] is None  # variable pricing is not a price
    public = [item for item in fake.requests if item["path"] == "/api/v1/models"][0]
    assert "Authorization" not in public["headers"]


def test_model_listing_without_key_reads_only_the_public_catalog(fake):
    listing = _gateway(fake, OPENROUTER_API_KEY="").list_models()
    assert listing["source"] == "public"
    assert [item["path"] for item in fake.requests] == ["/api/v1/models"]


def test_account_reports_key_usage_and_credits(fake):
    info = _gateway(fake).account()
    assert info["limit_remaining"] == 20.5 and info["usage_monthly"] == 4.5
    assert info["credits_remaining"] == 20.5 and info["total_credits"] == 50.0
    assert "label" not in info
    fake.credits_status = 403
    info = _gateway(fake).account()
    assert info["credits_remaining"] is None and "HTTP 403" in info["credits_note"]


def test_model_summary_ignores_malformed_rows():
    assert model_summary({"name": "no id"}) is None
    row = model_summary({"id": "x/y", "pricing": {"prompt": "abc"}, "context_length": True})
    assert row["prompt_usd_per_million"] is None and row["context_length"] is None


# -- secrecy -------------------------------------------------------------------


def test_key_never_appears_in_logs_errors_status_or_metrics(fake, caplog):
    caplog.set_level(logging.DEBUG)
    registry = MetricsRegistry()
    messages: list[str] = []
    gateway = _gateway(fake)
    gateway.generate(_request(), _context())
    # An upstream error that echoes the key back must be redacted.
    fake.chat_status = 400
    fake.chat_body = {"error": {"code": 400, "message": "bad key %s here" % FAKE_KEY}}
    for status in (400, 401, 402, 429, 500):
        fake.chat_status = status
        with pytest.raises(SonderError) as caught:
            gateway.generate(_request(), _context())
        messages.append(str(caught.value))
        gateway._cooldown_until = 0.0
    try:
        _gateway(fake, SONDER_ALLOW_CLOUD="0").account()
    except SonderError as exc:
        messages.append(str(exc))
    settings = gateway.settings()
    registry.observe_provider_usage("openrouter", upstream="DeepInfra", cost_usd=0.1,
                                    tokens={"prompt": 3})
    exported = [
        *messages, repr(settings), str(settings),
        json.dumps(gateway.provider_status(), default=str),
        json.dumps(dict(gateway.last_usage), default=str),
        json.dumps(gateway.list_models(), default=str),
        json.dumps(gateway.account(), default=str),
        caplog.text,
        registry.render().decode() if registry.enabled else "",
    ]
    for text in exported:
        assert FAKE_KEY not in text
        assert FAKE_KEY[9:25] not in text
    assert "[REDACTED]" in messages[0]


# Explicit fixture-owned values deliberately lack a credential-pattern shape.
_DUMMY_ERROR_KEY_32 = "fixture-only-" + "q" * 19
_DUMMY_ERROR_KEY_384 = "fixture-only-" + "q" * 371


@pytest.mark.parametrize("key, kind, suffix", [
    (_DUMMY_ERROR_KEY_32, _DUMMY_ERROR_KEY_32, " [[REDACTED]]"),
    (_DUMMY_ERROR_KEY_32, "worker:" + _DUMMY_ERROR_KEY_32, " [worker:[REDACTED]]"),
    ("echo", "echo-" * 12, ""),
], ids=["configured_kind", "embedded_kind", "expanded_kind_omitted"])
def test_error_kind_redacts_configured_credential(fake, key, kind, suffix):
    fake.chat_status = 500
    fake.chat_body = {"error": {"metadata": {"error_type": kind}}}
    gateway = _gateway(fake, OPENROUTER_API_KEY=key)
    with pytest.raises(DependencyUnavailable) as caught:
        gateway.generate(_request(), _context())
    assert str(caught.value) == "OpenRouter failed the request (HTTP 500)" + suffix
    assert key not in str(caught.value)
    assert len(fake.chat_requests()) == 1
    assert gateway.last_usage is None


@pytest.mark.parametrize("key, prefix, status, error", [
    (_DUMMY_ERROR_KEY_384, "", 400, InvalidInput),
    (_DUMMY_ERROR_KEY_32, "a" * 220, 500, DependencyUnavailable),
], ids=["long_configured_value", "cross_display_boundary"])
def test_error_message_redacts_before_display_bound(fake, key, prefix, status, error):
    fake.chat_status = status
    fake.chat_body = {"error": {"message": prefix + key + " harmless"}}
    gateway = _gateway(fake, OPENROUTER_API_KEY=key)
    with pytest.raises(error) as caught:
        gateway.generate(_request(), _context())
    detail = str(caught.value).split(": ", 1)[1]
    assert detail == prefix + "[REDACTED] harmless"
    assert len(detail) <= 240
    assert key not in str(caught.value) and key[:16] not in str(caught.value)
    assert len(fake.chat_requests()) == 1
    assert gateway.last_usage is None


@pytest.mark.parametrize("key, prefix", [
    (_DUMMY_ERROR_KEY_384, ""),
    (_DUMMY_ERROR_KEY_32, "a" * 220),
], ids=["long_configured_value", "cross_display_boundary"])
def test_stream_error_redacts_before_display_bound(fake, observed, monkeypatch, tmp_path, key, prefix):
    usage = []
    observer = Observer()
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    monkeypatch.setattr(provider_attempts, "_attempt_observer", observer)
    fake.stream_events = [
        "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
        for text in ("firstλ", "secondμ")
    ] + ["data: " + json.dumps({"error": {"message": prefix + key + " harmless"}})]
    gateway = _gateway(fake, OPENROUTER_API_KEY=key)
    repository, capture, request, pending = _capture(tmp_path)
    chunks = []
    stream = gateway.stream(request, _context())
    try:
        with provider_attempts.provider_attempt_scope(capture, pending):
            with pytest.raises(DependencyUnavailable) as caught:
                for chunk in stream:
                    chunks.append(chunk)
        events = repository.read_range("session")
    finally:
        stream.close()
        assert repository.close()
    assert str(caught.value) == "OpenRouter stream failed mid-response: " + prefix + "[REDACTED] harmless"
    assert key not in str(caught.value) and key[:16] not in str(caught.value)
    assert [chunk.text for chunk in chunks] == ["firstλ", "secondμ"]
    assert usage == [] and gateway.last_usage is None
    assert [event.event_type for event in events] == [
        "model.requested", "provider.requested", "provider.failed",
    ]
    assert set(events[-1].payload) == {"turn_id", "request_id", "attempt_id", "error_code"}
    assert events[-1].payload["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert events[-1].payload["attempt_id"] == events[-2].payload["attempt_id"]
    assert observer.finishes == [{"error_code": "DEPENDENCY_UNAVAILABLE"}]
    evidence = json.dumps({
        "events": [{"event_type": event.event_type, "payload": dict(event.payload)} for event in events],
        "observer_starts": observer.starts, "observer_finishes": observer.finishes,
    }, sort_keys=True)
    assert key not in evidence and key[:16] not in evidence
    assert len(fake.chat_requests()) == len(observer.starts) == len(observer.finishes) == 1
    assert observed.queues[0].high_water <= 64
    assert not observed.workers[0].is_alive()


@pytest.mark.parametrize("message, kind, suffix", [
    ("ordinary unavailable", "upstream_failure", ": ordinary unavailable [upstream_failure]"),
    ("hello \n λ", "benign", ": hello λ [benign]"),
    ("a" * 240, None, ": " + "a" * 240),
    ("a" * 241, None, ": " + "a" * 237 + "..."),
    (17, 19, ""),
    (None, "k" * 65, ""),
    (None, "k" * 64, " [" + "k" * 64 + "]"),
    ("", None, ""),
], ids=["benign", "unicode_whitespace", "at_display_limit", "over_display_limit",
        "non_string_fields", "oversized_kind", "kind_at_limit", "empty_fields"])
def test_error_detail_compatibility(fake, message, kind, suffix):
    fake.chat_status = 500
    fake.chat_body = {"error": {"message": message, "metadata": {"error_type": kind}}}
    gateway = _gateway(fake, OPENROUTER_API_KEY=_DUMMY_ERROR_KEY_32)
    with pytest.raises(DependencyUnavailable) as caught:
        gateway.generate(_request(), _context())
    assert str(caught.value) == "OpenRouter failed the request (HTTP 500)" + suffix
    assert len(fake.chat_requests()) == 1
    assert gateway.last_usage is None


@pytest.mark.parametrize("streaming", [False, True], ids=["http_error_budget", "sse_line_budget"])
def test_error_sanitation_size_controls(fake, observed, monkeypatch, streaming):
    from sonder_runtime.adapters.inference.openai_compat_gateway import ERROR_BODY_LIMIT
    from sonder_runtime.adapters.inference.openrouter_gateway import _STREAM_LINE_LIMIT

    key = _DUMMY_ERROR_KEY_32
    message = "large " + key + " "
    document = {"error": {"message": message}}
    budget = _STREAM_LINE_LIMIT - 1 if streaming else ERROR_BODY_LIMIT - 1
    framing = len(b"data: \n") if streaming else 0
    size = len(json.dumps(document).encode()) + framing
    document["error"]["message"] += "a" * (budget - size)
    assert len(json.dumps(document).encode()) + framing == budget
    gateway = _gateway(fake, OPENROUTER_API_KEY=key)
    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    chunks = []
    if streaming:
        fake.stream_events = [
            'data: {"choices":[{"delta":{"content":"prefix"}}]}',
            "data: " + json.dumps(document),
        ]
        stream = gateway.stream(_request(), _context())
        try:
            with pytest.raises(DependencyUnavailable) as caught:
                for chunk in stream:
                    chunks.append(chunk)
        finally:
            stream.close()
        assert [chunk.text for chunk in chunks] == ["prefix"]
        assert observed.queues[0].high_water <= 64
        assert not observed.workers[0].is_alive()
    else:
        fake.chat_status, fake.chat_body = 500, document
        with pytest.raises(DependencyUnavailable) as caught:
            gateway.generate(_request(), _context())
    detail = str(caught.value).split(": ", 1)[1]
    assert detail == "large [REDACTED] " + "a" * 220 + "..."
    assert len(detail) == 240 and key not in str(caught.value)
    assert usage == [] and gateway.last_usage is None
    assert len(fake.chat_requests()) == 1


def test_openrouter_keys_are_covered_by_redaction():
    assert FAKE_KEY not in redact_text("using %s now" % FAKE_KEY)
    assert FAKE_KEY not in redact_text("OPENROUTER_API_KEY=%s" % FAKE_KEY)


def test_openrouter_key_is_scrubbed_from_child_processes():
    from sonder_runtime.platform.logging import child_environment

    assert "OPENROUTER_API_KEY" not in child_environment({"OPENROUTER_API_KEY": FAKE_KEY, "PATH": "x"})


def test_upstream_metric_labels_are_bounded():
    registry = MetricsRegistry()
    assert registry.upstream_label("Google Vertex") == "google-vertex"
    assert registry.upstream_label("") == "unknown"
    assert registry.upstream_label("<script>") == "other"
    for index in range(40):
        registry.upstream_label("host-%d" % index)
    assert registry.upstream_label("host-39") == "other"


# -- conformance probes --------------------------------------------------------


def _probe_identity():
    return BackendIdentity(
        backend="openai-compatible", model=MODEL, model_digest="a" * 64,
        quantization="unknown", backend_version="openrouter", tokenizer_digest="b" * 64,
        template_digest="c" * 64, context_tokens=200000, hardware="hosted",
    )


def test_protocol_probe_on_the_real_route_needs_explicit_cloud():
    """Against the default https endpoint nothing is sent unless cloud is allowed."""
    sent = []

    def transport(url, payload, headers, timeout):
        sent.append((url, payload))
        return _chat('{"tool":"echo","arguments":{"value":"alpha"}}')

    env = {"SONDER_ALLOW_CLOUD": "1", "OPENROUTER_API_KEY": FAKE_KEY}
    gateway = protocol_probe_gateway(MODEL, env=env, transport=transport)
    identity = _probe_identity()
    refused = OpenAICompatibleProtocolProbe(gateway, identity_reader=lambda: identity).run(timeout_seconds=10)
    assert sent == []  # cloud_allowed defaults to False: the consent gate refused
    assert all(result.passed is not True for result in refused.results)
    OpenAICompatibleProtocolProbe(
        gateway, identity_reader=lambda: identity, cloud_allowed=True,
    ).run(timeout_seconds=10)
    assert sent and all(url == "https://openrouter.ai/api/v1/chat/completions" for url, _ in sent)
    assert all(payload["provider"]["data_collection"] == "deny" for _, payload in sent)
    with pytest.raises(Forbidden):
        protocol_probe_gateway(MODEL, env={**env, "SONDER_ALLOW_CLOUD": "0"})


def test_protocol_probe_reaches_the_fake_openrouter_route(fake):
    fake.chat_body = _chat('{"tool":"echo","arguments":{"value":"alpha"}}')
    gateway = protocol_probe_gateway(MODEL, env=_env(fake))
    identity = _probe_identity()
    record = OpenAICompatibleProtocolProbe(
        gateway, identity_reader=lambda: identity, cloud_allowed=True,
    ).run(timeout_seconds=10)
    assert fake.chat_requests(), "the probe reached the OpenRouter route"
    assert all(item["body"]["provider"]["zdr"] is True for item in fake.chat_requests())
    assert all(item["headers"]["Authorization"] == "Bearer " + FAKE_KEY for item in fake.chat_requests())
    passed = {result.capability.value for result in record.results if result.passed is True}
    assert "chat" in passed


def test_settings_repr_hides_key():
    settings = OpenRouterSettings(api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(settings)


def test_error_sanitation_small_stress(observed, monkeypatch, caplog):
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    usage = []
    monkeypatch.setattr("sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append)
    active_lock = threading.Lock()
    active = peak = 0

    @contextmanager
    def peer(category, key, prefix, texts):
        state = FakeOpenRouter()
        # Set one response before starting the peer; never change it concurrently.
        if category == "kind":
            state.chat_status = 500
            state.chat_body = {"error": {"metadata": {"error_type": key}}}
        elif category == "http":
            state.chat_status = 500
            state.chat_body = {"error": {"message": prefix + key + " harmless"}}
        else:
            state.stream_events = [
                "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
                for text in texts
            ] + ["data: " + json.dumps({"error": {"message": prefix + key + " harmless"}})]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                state.handle(self, "POST")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        state.base_url = "http://127.0.0.1:%d/api/v1" % server.server_port
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            assert not thread.is_alive(), "synthetic loopback peer did not drain"

    def exchange(index):
        nonlocal active, peak
        category = ("kind", "http", "sse")[index % 3]
        short_key = "fixture-only-" + ("%019d" % index)
        # Kind retains its existing 64-character input cap. Other categories
        # alternate a 32-character boundary key and a 384-character value.
        key = short_key if category == "kind" or index % 2 == 0 else short_key + "q" * 352
        prefix = "a" * 220 if len(key) == 32 else ""
        texts = tuple("%dλ" % number for number in range(128))
        with active_lock:
            active += 1
            peak = max(peak, active)
            assert active <= 2
        try:
            with peer(category, key, prefix, texts) as state:
                gateway = _gateway(state, OPENROUTER_API_KEY=key)
                chunks = []
                if category == "sse":
                    stream = gateway.stream(_request(prompt="synthetic stress prompt"), _context())
                    try:
                        with pytest.raises(DependencyUnavailable) as caught:
                            for chunk in stream:
                                chunks.append(chunk)
                    finally:
                        stream.close()
                    assert [chunk.text for chunk in chunks] == list(texts)
                    assert all(chunk.text and chunk.finish_reason is None for chunk in chunks)
                    expected = "OpenRouter stream failed mid-response: " + prefix + "[REDACTED] harmless"
                else:
                    with pytest.raises(DependencyUnavailable) as caught:
                        gateway.generate(_request(prompt="synthetic stress prompt"), _context())
                    expected = "OpenRouter failed the request (HTTP 500)"
                    expected += " [[REDACTED]]" if category == "kind" else ": " + prefix + "[REDACTED] harmless"
                detail = str(caught.value)
                assert detail == expected
                assert key not in detail and short_key[:16] not in detail
                assert gateway.last_usage is None
                (sent,) = state.chat_requests()
                assert sent["method"] == "POST"
                assert sent["body"]["stream"] is (category == "sse")
                assert sent["body"]["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}
                assert len(state.requests) == 1  # no retry or hidden route request
                return category
        finally:
            with active_lock:
                active -= 1

    # At most two tasks are submitted at once; no unbounded future backlog.
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for first in range(0, 120, 2):
            futures = [pool.submit(exchange, index) for index in (first, first + 1)]
            results.extend(future.result(timeout=35) for future in futures)
    assert Counter(results) == {"kind": 40, "http": 40, "sse": 40}
    assert active == 0 and 1 <= peak <= 2
    assert usage == []
    assert len(observed.queues) == len(observed.workers) == 40
    assert all(item.maxsize == 64 and item.high_water <= 64 for item in observed.queues)
    for worker in observed.workers:
        worker.join(3)
        assert not worker.is_alive(), "synthetic stream worker did not drain"
    assert "fixture-only-" not in caplog.text
