"""AnthropicGateway against a fake SDK client: no network, no real key.

The SDK is never imported here (it is an optional dependency): tests inject a
``client_factory`` whose clients mimic the two ``anthropic`` surfaces the
gateway uses (``messages.create`` and ``messages.stream``) and raise errors
shaped like ``anthropic.APIStatusError`` (``status_code``, ``message``,
``response.headers``).  The key is a sentinel value, deliberately outside every
credential-pattern shape, whose absence from errors, logs, status documents,
usage records and metrics is asserted by value.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference.anthropic_gateway import (
    AnthropicCreditsExhausted,
    AnthropicDailyCapReached,
    AnthropicGateway,
    AnthropicRateLimited,
    AnthropicSdkMissing,
    AnthropicSettings,
    config_from_env,
)
from sonder_runtime.adapters.inference.anthropic_spend import SpendLedger, SpendLedgerUnreadable
from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.adapters.provider_bindings import (
    PROVIDER_LABEL_IDS,
    ProviderBindings,
    provider_bindings_from_env,
    provider_id_for_label,
)
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain import anthropic_policy
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    SonderError,
)
from sonder_runtime.platform.logging import SECRET_ENV_VARS, Redactor, child_environment
from sonder_runtime.platform.metrics import MetricsRegistry

KEY_1 = "fixture-only-anthropic-key-one-" + "a" * 20
KEY_2 = "fixture-only-anthropic-key-two-" + "b" * 20
CREDIT_MESSAGE = "Your credit balance is too low to access the Anthropic API."


class FakeApiError(Exception):
    """Shaped like ``anthropic.APIStatusError`` (duck-typed by the gateway)."""

    def __init__(self, status, message, headers=None):
        super().__init__("%s %s" % (status, message))
        self.status_code = status
        self.message = message
        self.response = SimpleNamespace(headers=dict(headers or {}))


class APITimeoutError(Exception):
    """Same class name as the SDK's timeout error; no status code."""


def _message(text="hello from claude", *, model="claude-opus-5-5", input_tokens=12,
             output_tokens=5, cache_read=0, cache_write=0, stop_reason="end_turn",
             inference_geo=None, plan=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="hidden"),
                 SimpleNamespace(type="text", text=text)],
        model=model, stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
            inference_geo=inference_geo,
        ),
        plan=plan,
    )


class FakeSdk:
    """A scriptable stand-in for the ``anthropic`` client; records every call."""

    def __init__(self, behave=None):
        self.behave = behave or (lambda key, params: _message())
        self.calls: list[dict] = []
        self.builds: list[float] = []  # one entry per client built
        self.call_timeouts: list[float] = []
        self.closed = 0

    def factory(self, key, timeout):
        sdk = self
        self.builds.append(timeout)

        class Messages:
            def create(self, **params):
                sdk.call_timeouts.append(params.pop("timeout", None))
                sdk.calls.append({"key": key, "params": params, "stream": False})
                return sdk.behave(key, params)

            def stream(self, **params):
                sdk.call_timeouts.append(params.pop("timeout", None))
                sdk.calls.append({"key": key, "params": params, "stream": True})
                outcome = sdk.behave(key, params)  # raises here for connect-time errors
                return _FakeStream(outcome)

        def close():
            sdk.closed += 1

        return SimpleNamespace(messages=Messages(), close=close)

    @property
    def keys_used(self):
        return [call["key"] for call in self.calls]


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        plan = self._message.plan or {}
        for block in self._message.content:
            if block.type == "text":
                for index, piece in enumerate((block.text[:5], block.text[5:])):
                    if index == 1:
                        if plan.get("gate") is not None:
                            plan["gate"].wait(5)
                        if plan.get("fail") is not None:
                            raise plan["fail"]
                    if piece:
                        yield piece

    @property
    def current_message_snapshot(self):
        plan = self._message.plan or {}
        if plan.get("before_start"):
            raise AssertionError  # the SDK asserts before message_start
        return self._message

    def get_final_message(self):
        return self._message


def _settings(**overrides):
    values = dict(api_key_1=KEY_1, api_key_2=KEY_2, cloud_enabled=True)
    values.update(overrides)
    return AnthropicSettings(**values)


def _context(*, cloud=True):
    return local_owner_context(
        correlation_id="anthropic-test", timeout_seconds=30, cloud_allowed=cloud,
    )


def _request(prompt="say hi", **kwargs):
    return ModelRequest(prompt=prompt, tier=kwargs.pop("tier", "general"), **kwargs)


@pytest.fixture
def ledger(tmp_path):
    return SpendLedger(tmp_path / "anthropic-spend.json")


def _gateway(sdk, ledger, settings=None, **kwargs):
    return AnthropicGateway(
        settings or _settings(), client_factory=sdk.factory, spend_ledger=ledger,
        request_admission=HostModelRequestAdmission(), **kwargs,
    )


# -- consent ----------------------------------------------------------------------


def test_refuses_without_the_cloud_opt_in_and_never_calls_the_sdk(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger, _settings(cloud_enabled=False))
    with pytest.raises(Forbidden, match="SONDER_ALLOW_CLOUD"):
        gateway.generate(_request(), _context())
    assert sdk.calls == []


def test_env_without_allow_cloud_resolves_to_a_refusing_gateway(ledger):
    env = {"SONDER_ANTHROPIC_API_KEY": KEY_1}
    assert config_from_env(env).cloud_enabled is False
    sdk = FakeSdk()
    gateway = AnthropicGateway(
        env=env, client_factory=sdk.factory, spend_ledger=ledger,
        request_admission=HostModelRequestAdmission(),
    )
    with pytest.raises(Forbidden):
        gateway.generate(_request(), _context())
    assert sdk.calls == []


def test_refuses_when_the_operation_context_does_not_allow_cloud(ledger):
    sdk = FakeSdk()
    with pytest.raises(Forbidden, match="does not allow prompts to leave"):
        _gateway(sdk, ledger).generate(_request(), _context(cloud=False))
    assert sdk.calls == []


def test_a_missing_key_is_an_actionable_error_and_plain_anthropic_api_key_is_not_read(ledger):
    env = {"SONDER_ALLOW_CLOUD": "1", "ANTHROPIC_API_KEY": "must-not-be-read"}
    assert config_from_env(env).key_slots == ()
    sdk = FakeSdk()
    gateway = AnthropicGateway(
        env=env, client_factory=sdk.factory, spend_ledger=ledger,
        request_admission=HostModelRequestAdmission(),
    )
    with pytest.raises(InvalidInput, match="SONDER_ANTHROPIC_API_KEY"):
        gateway.generate(_request(), _context())
    assert sdk.calls == []


# -- the happy path and request mapping ---------------------------------------------


def test_generate_maps_the_request_onto_messages_and_returns_text(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger)
    request = _request(
        "and now?", system="be brief",
        history=({"role": "user", "content": "hi"}, ("assistant", "hello"),
                 {"role": "system", "content": "extra rule"}),
        options={"temperature": 0.9, "top_p": 0.5, "num_ctx": 4096, "num_predict": 200,
                 "stop": "END"},
    )
    response = gateway.generate(request, _context())
    assert isinstance(response, ModelResponse)
    assert response.text == "hello from claude"
    assert response.model == "claude-opus-5-5"
    assert (response.tokens_in, response.tokens_out) == (12, 5)
    (call,) = sdk.calls
    params = call["params"]
    assert params["model"] == "claude-opus-5-5"
    assert params["system"] == "be brief\n\nextra rule"
    assert params["messages"] == [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "and now?"},
    ]
    assert params["max_tokens"] == 200
    assert params["stop_sequences"] == ["END"]
    assert not ({"temperature", "top_p", "top_k"} & set(params))
    assert call["key"] == KEY_1


def test_max_tokens_is_bounded_by_the_configured_ceiling(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger, _settings(max_tokens_ceiling=300))
    gateway.generate(_request(options={"num_predict": 99_999}), _context())
    gateway.generate(_request(), _context())
    assert [call["params"]["max_tokens"] for call in sdk.calls] == [300, 300]
    assert config_from_env({"SONDER_ANTHROPIC_MAX_TOKENS": "777"}).max_tokens_ceiling == 777
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_ANTHROPIC_MAX_TOKENS": "0"})


def test_model_selection_option_then_tier_then_env_then_documented_default(ledger):
    sdk = FakeSdk(lambda key, params: _message(model=params["model"]))
    gateway = _gateway(sdk, ledger, _settings(
        model="claude-sonnet-5-5", tier_models={"fast": "claude-haiku-5-5"},
    ))
    gateway.generate(_request(tier="fast"), _context())
    gateway.generate(_request(tier="code"), _context())
    gateway.generate(_request(tier="code", options={"model": "claude-fable-5-2"}), _context())
    assert [call["params"]["model"] for call in sdk.calls] == [
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-fable-5-2",
    ]
    assert config_from_env({}).model == anthropic_policy.DEFAULT_MODEL == "claude-opus-5-5"
    assert config_from_env({"SONDER_ANTHROPIC_MODEL": "claude-sonnet-5-5"}).model == "claude-sonnet-5-5"
    assert config_from_env(
        {"SONDER_ANTHROPIC_TIER_MODELS": "fast=claude-haiku-5-5"}
    ).model_for_tier("fast") == "claude-haiku-5-5"


def test_an_unpriced_model_is_refused_before_any_send(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger)
    with pytest.raises(InvalidInput, match="price table"):
        gateway.generate(_request(options={"model": "claude-made-up-9"}), _context())
    with pytest.raises(InvalidInput):
        gateway.generate(_request(options={"model": "bad model/id"}), _context())
    assert sdk.calls == []


@pytest.mark.parametrize("options", [
    {"tools": [{"name": "x"}]}, {"tool_choice": "auto"}, {"think": True},
    {"format": "json"}, {"num_predict": 0}, {"num_predict": True}, {"stop": ["a"] * 5},
])
def test_options_the_gateway_cannot_carry_are_refused_not_dropped(ledger, options):
    sdk = FakeSdk()
    with pytest.raises(InvalidInput):
        _gateway(sdk, ledger).generate(_request(options=options), _context())
    assert sdk.calls == []


def test_embeddings_are_not_served(ledger):
    with pytest.raises(DependencyUnavailable, match="embeddings"):
        _gateway(FakeSdk(), ledger).embed(["x"], _context())


# -- key failover -----------------------------------------------------------------


def _fail_key_one(error):
    def behave(key, params):
        if key == KEY_1:
            raise error
        return _message()
    return behave


@pytest.mark.parametrize("error", [
    FakeApiError(401, "invalid x-api-key"),
    FakeApiError(400, CREDIT_MESSAGE),
    FakeApiError(400, "  your CREDIT balance\nis too  low to access the API  "),
], ids=["401", "credit_400", "credit_400_whitespace_and_case"])
def test_key_one_refusal_fails_over_to_key_two_exactly_once(ledger, error):
    sdk = FakeSdk(_fail_key_one(error))
    gateway = _gateway(sdk, ledger)
    response = gateway.generate(_request(), _context())
    assert response.text == "hello from claude"
    assert sdk.keys_used == [KEY_1, KEY_2]
    assert gateway.last_usage["key_slot"] == 2
    assert ledger.snapshot()["key_slot_calls"] == {"1": 0, "2": 1}


def test_key_two_is_never_tried_first_and_key_one_serves_when_healthy(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger)
    gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1]
    assert gateway.last_usage["key_slot"] == 1
    assert ledger.snapshot()["key_slot_calls"] == {"1": 1, "2": 0}


@pytest.mark.parametrize("error, expected", [
    (FakeApiError(500, "internal"), DependencyUnavailable),
    (FakeApiError(529, "Overloaded"), DependencyUnavailable),
    (FakeApiError(429, "slow down", {"retry-after": "7"}), AnthropicRateLimited),
    (FakeApiError(400, "messages: roles must alternate"), InvalidInput),
    (FakeApiError(403, "no permission"), Forbidden),
    (FakeApiError(404, "model: nope"), InvalidInput),
], ids=["500", "529", "429", "other_400", "403", "404"])
def test_other_errors_never_fail_over_and_stay_single_attempt(ledger, error, expected):
    sdk = FakeSdk(_fail_key_one(error))
    gateway = _gateway(sdk, ledger)
    with pytest.raises(expected):
        gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1]  # one attempt, key two untouched
    assert ledger.snapshot()["calls"] == 0  # a failed call is never charged


def test_both_keys_refused_raises_after_exactly_two_attempts(ledger):
    sdk = FakeSdk(lambda key, params: (_ for _ in ()).throw(FakeApiError(401, "bad")))
    with pytest.raises(Forbidden, match="slot 2"):
        _gateway(sdk, ledger).generate(_request(), _context())
    assert sdk.keys_used == [KEY_1, KEY_2]


def test_credit_exhaustion_on_the_only_key_is_a_typed_actionable_error(ledger):
    sdk = FakeSdk(lambda key, params: (_ for _ in ()).throw(FakeApiError(400, CREDIT_MESSAGE)))
    gateway = _gateway(sdk, ledger, _settings(api_key_2=""))
    with pytest.raises(AnthropicCreditsExhausted, match="add credit"):
        gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1]


def test_a_429_starts_a_local_cooldown_from_retry_after(ledger):
    clock = [100.0]
    sdk = FakeSdk(lambda key, params: (_ for _ in ()).throw(
        FakeApiError(429, "slow", {"retry-after": "30"})))
    gateway = _gateway(sdk, ledger, monotonic=lambda: clock[0])
    with pytest.raises(AnthropicRateLimited) as first:
        gateway.generate(_request(), _context())
    assert first.value.retry_after == 30.0
    with pytest.raises(AnthropicRateLimited, match="not sending"):
        gateway.generate(_request(), _context())
    assert len(sdk.calls) == 1  # the second call never reached the SDK
    clock[0] += 31.0
    with pytest.raises(AnthropicRateLimited):
        gateway.generate(_request(), _context())
    assert len(sdk.calls) == 2


def test_timeouts_and_connection_failures_map_to_domain_errors(ledger):
    gateway = _gateway(FakeSdk(lambda k, p: (_ for _ in ()).throw(APITimeoutError("t"))), ledger)
    with pytest.raises(DeadlineExceeded):
        gateway.generate(_request(), _context())
    gateway = _gateway(FakeSdk(lambda k, p: (_ for _ in ()).throw(ConnectionResetError("boom"))), ledger)
    with pytest.raises(DependencyUnavailable, match="cannot reach Anthropic"):
        gateway.generate(_request(), _context())


def test_a_refusal_stop_reason_is_a_typed_error_but_the_usage_is_still_billed(ledger):
    sdk = FakeSdk(lambda k, p: _message(text="", stop_reason="refusal"))
    with pytest.raises(Forbidden, match="declined"):
        _gateway(sdk, ledger).generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 1


# -- the SDK is optional and never allowed to retry --------------------------------


def test_missing_sdk_is_a_typed_actionable_error_before_any_spend_or_send(ledger, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` fail
    gateway = AnthropicGateway(
        _settings(), spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
    )
    with pytest.raises(AnthropicSdkMissing) as caught:
        gateway.generate(_request(), _context())
    assert "pip install anthropic" in str(caught.value)
    assert isinstance(caught.value, DependencyUnavailable)
    assert ledger.snapshot()["calls"] == 0
    with pytest.raises(AnthropicSdkMissing):
        list(gateway.stream(_request(), _context()))


def test_default_client_is_the_official_sdk_with_its_own_retries_disabled(ledger, monkeypatch):
    built = []

    calls = []

    class Anthropic:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.api_key, self.auth_token = kwargs["api_key"], None
            self.base_url = kwargs["base_url"] + "/"
            self._custom_headers = {"X-From-Env": "1"}
            self.messages = SimpleNamespace(
                create=lambda **params: calls.append(params) or _message())

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Anthropic))
    gateway = AnthropicGateway(
        _settings(), spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
    )
    gateway.generate(_request(), _context())
    (kwargs,) = built
    assert kwargs["api_key"] == KEY_1
    assert kwargs["max_retries"] == 0  # the SDK must not silently re-send billed work
    assert kwargs["base_url"] == "https://api.anthropic.com"  # never the SDK's env-derived URL
    assert kwargs["timeout"] == 300.0
    assert 0 < calls[0]["timeout"] <= 30  # per-call, bounded by the operation deadline
    assert gateway._clients[1][2]._custom_headers == {}  # ANTHROPIC_CUSTOM_HEADERS is dropped


# -- accounting and the daily cap ---------------------------------------------------


def test_cost_is_computed_from_usage_with_the_documented_price_table(ledger):
    sdk = FakeSdk(lambda k, p: _message(
        model="claude-sonnet-5-5", input_tokens=1_000, output_tokens=500,
        cache_read=2_000, cache_write=100))
    gateway = _gateway(sdk, ledger, _settings(model="claude-sonnet-5-5"))
    gateway.generate(_request(), _context())
    # sonnet 5.5: $2 in / $10 out, cache read $0.10, 5-minute write 1.25x ($2.50)
    expected = Decimal(1_000 * 2 + 500 * 10 + 2_000 * Decimal("0.10") + 100 * Decimal("2.5")) / 1_000_000
    assert expected == Decimal("0.00745")
    assert gateway.last_usage["cost_usd"] == expected
    assert Decimal(ledger.snapshot()["usd"]) == expected
    assert gateway.last_usage["prompt_tokens"] == 3_100
    assert gateway.last_usage["model"] == "claude-sonnet-5-5"


def test_price_table_rows_and_the_long_prompt_tier():
    cost = anthropic_policy.usage_cost_usd
    assert cost("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal(24)
    assert cost("claude-fable-5-2", input_tokens=1_000_000, output_tokens=1_000_000) == Decimal(48)
    assert cost("claude-haiku-5-5", input_tokens=100_000, output_tokens=0) == Decimal("0.01")
    assert cost("claude-haiku-5-5", input_tokens=100_001, output_tokens=1_000_000) == Decimal("2.550001")
    assert cost("claude-opus-5-5-20260101", input_tokens=1_000_000, output_tokens=0) == Decimal(4)
    with pytest.raises(anthropic_policy.AnthropicPolicyError):
        cost("claude-nonesuch", input_tokens=1, output_tokens=1)


def test_usage_is_exported_with_backend_anthropic_and_the_model(ledger, caplog):
    caplog.set_level(logging.INFO)
    sdk = FakeSdk()
    _gateway(sdk, ledger).generate(_request(), _context())
    assert "anthropic usage: model=claude-opus-5-5 key_slot=1" in caplog.text
    registry = MetricsRegistry()
    registry.observe_provider_usage("anthropic", upstream="anthropic", cost_usd=0.5,
                                    tokens={"prompt": 3})
    if registry.enabled:
        rendered = registry.render().decode()
        assert 'backend="anthropic"' in rendered


def test_daily_cap_refuses_the_next_call_after_spend_crosses_it_and_persists(ledger, tmp_path):
    # opus 5.5: 1000 in + 1000 out = $0.024 per call; worst case for max_tokens 1000 is ~$0.0223
    sdk = FakeSdk(lambda k, p: _message(input_tokens=1_000, output_tokens=1_000))
    settings = _settings(daily_usd=Decimal("0.047"))
    gateway = _gateway(sdk, ledger, settings)
    gateway.generate(_request(options={"num_predict": 1000}), _context())   # worst case fits the cap: allowed
    gateway.generate(_request(options={"num_predict": 1000}), _context())   # $0.024 + ~0.0222 worst case fits 0.047: allowed, ends at $0.048 (over the cap)
    assert Decimal(ledger.snapshot()["usd"]) == Decimal("0.048")
    with pytest.raises(AnthropicDailyCapReached) as caught:
        gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert len(sdk.calls) == 2  # the refused call never reached the SDK
    message = str(caught.value)
    assert "$0.0480" in message and "$0.0470" in message and "SONDER_ANTHROPIC_DAILY_USD" in message
    assert caught.value.cap_usd == Decimal("0.047") and caught.value.spent_usd == Decimal("0.048")
    assert not isinstance(caught.value, CapacityExceeded)  # a hard stop, not a retryable 429
    # A brand new gateway and ledger object over the same file still refuses.
    fresh = _gateway(sdk, SpendLedger(tmp_path / "anthropic-spend.json"), settings)
    with pytest.raises(AnthropicDailyCapReached):
        fresh.generate(_request(options={"num_predict": 1000}), _context())
    with pytest.raises(AnthropicDailyCapReached):
        list(fresh.stream(_request(options={"num_predict": 1000}), _context()))
    assert len(sdk.calls) == 2


def test_the_cap_is_per_utc_day_and_defaults_to_ten_dollars(tmp_path):
    clock = [1_800_000_000.0]
    ledger = SpendLedger(tmp_path / "spend.json", clock=lambda: clock[0])
    ledger.record(usd=Decimal("9.99"), slot=1, input_tokens=1, output_tokens=1)
    assert ledger.today_usd() == Decimal("9.99")
    clock[0] += 86_400
    assert ledger.today_usd() == Decimal(0)
    assert config_from_env({}).daily_usd == Decimal(10)
    assert config_from_env({"SONDER_ANTHROPIC_DAILY_USD": "2.5"}).daily_usd == Decimal("2.5")
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_ANTHROPIC_DAILY_USD": "-1"})
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_ANTHROPIC_DAILY_USD": "lots"})


def test_a_zero_cap_refuses_every_call(ledger):
    sdk = FakeSdk()
    with pytest.raises(AnthropicDailyCapReached):
        _gateway(sdk, ledger, _settings(daily_usd=Decimal(0))).generate(_request(), _context())
    assert sdk.calls == []


def test_an_unreadable_ledger_fails_closed_instead_of_resetting_spend(tmp_path, ledger):
    (tmp_path / "anthropic-spend.json").write_text("{not json", encoding="utf-8")
    sdk = FakeSdk()
    with pytest.raises(SpendLedgerUnreadable):
        _gateway(sdk, ledger).generate(_request(), _context())
    assert sdk.calls == []


def test_a_ledger_write_failure_never_loses_a_billed_response(tmp_path):
    blocked = tmp_path / "is-a-file"
    blocked.write_text("x", encoding="utf-8")
    ledger = SpendLedger(blocked / "nested" / "spend.json")  # the parent is a file: writes fail
    ledger.record(usd=Decimal("1.5"), slot=1, input_tokens=1, output_tokens=1)  # must not raise
    assert ledger._unpersisted  # counted in memory for this process


# -- secrecy ------------------------------------------------------------------------


def test_key_never_appears_in_errors_logs_status_usage_or_metrics(ledger, caplog):
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    for status in (400, 401, 402, 403, 404, 429, 500, 529):
        def behave(key, params, status=status):
            # A hostile upstream message that echoes both keys back.
            raise FakeApiError(status, "echo %s / %s" % (KEY_1, KEY_2), {"retry-after": "1"})

        with pytest.raises(SonderError) as caught:
            _gateway(FakeSdk(behave), ledger).generate(_request(), _context())
        texts += [str(caught.value), repr(caught.value),
                  "".join(traceback.format_exception(caught.value))]
    ok = _gateway(FakeSdk(), ledger)
    ok.generate(_request(), _context())
    registry = MetricsRegistry()
    registry.observe_provider_usage("anthropic", upstream="anthropic", cost_usd=0.1, tokens={"prompt": 3})
    settings = ok.settings()
    texts += [
        repr(settings), str(settings), json.dumps(ok.provider_status(), default=str),
        json.dumps(dict(ok.last_usage), default=str), json.dumps(ledger.snapshot()),
        (ledger.path).read_text(encoding="utf-8"), caplog.text,
        registry.render().decode() if registry.enabled else "",
        repr(ok.capability_health()), json.dumps(ok.served_tier_models()),
    ]
    assert any("[REDACTED]" in text for text in texts)
    for text in texts:
        for key in (KEY_1, KEY_2):
            assert key not in text
            assert key[10:40] not in text


def test_stream_errors_never_carry_the_key_either(ledger, caplog):
    caplog.set_level(logging.DEBUG)

    def behave(key, params):
        raise FakeApiError(500, "echo %s" % KEY_1)

    with pytest.raises(DependencyUnavailable) as caught:
        list(_gateway(FakeSdk(behave), ledger).stream(_request(), _context()))
    assert KEY_1 not in str(caught.value) + caplog.text
    assert "[REDACTED]" in str(caught.value)


def test_status_reports_key_slots_as_booleans_and_spend_against_the_cap(ledger):
    gateway = _gateway(FakeSdk(), ledger, _settings(api_key_2="", daily_usd=Decimal(5)))
    gateway.generate(_request(), _context())
    entry = gateway.provider_status()["anthropic"]
    assert entry["state"] == "configured" and entry["healthy"] is True
    assert entry["key_slots"] == {"1": True, "2": False}
    assert entry["daily_cap_usd"] == "5"
    assert Decimal(entry["spent_today_usd"]) > 0 and entry["calls_today"] == 1
    assert entry["cap_reached"] is False
    assert KEY_1 not in json.dumps(entry)
    unconfigured = AnthropicGateway(
        env={}, client_factory=FakeSdk().factory, spend_ledger=ledger,
        request_admission=HostModelRequestAdmission(),
    ).provider_status()["anthropic"]
    assert unconfigured["state"] == "unavailable" and unconfigured["configured"] is False


def test_status_cli_prints_no_key_and_makes_no_call(ledger, monkeypatch, capsys):
    from sonder_runtime.bootstrap import anthropic_cli

    monkeypatch.setenv("SONDER_ANTHROPIC_API_KEY", KEY_1)
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(anthropic_cli, "AnthropicGateway", lambda: AnthropicGateway(
        spend_ledger=ledger, request_admission=HostModelRequestAdmission()))
    assert anthropic_cli.cmd_anthropic(SimpleNamespace(json=False)) == 0
    output = capsys.readouterr().out
    assert "key slot 1: set | key slot 2: not set" in output
    assert KEY_1 not in output
    assert anthropic_cli.cmd_anthropic(SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["key_slots"] == {"1": True, "2": False}


def test_both_key_names_are_in_the_logging_redaction_list_and_child_scrub(monkeypatch):
    for name in ("SONDER_ANTHROPIC_API_KEY", "SONDER_ANTHROPIC_API_KEY_2"):
        assert name in SECRET_ENV_VARS
        assert child_environment({name: "value-1234", "PATH": "p"}) == {"PATH": "p"}
        assert Redactor(env={name: KEY_1}).redact("failed with %s here" % KEY_1) == "failed with [REDACTED] here"
    # The test harness scrub covers the SONDER_ prefix; the live-capture list names
    # them explicitly so the opt-in live test can restore them.
    import conftest

    for name in ("SONDER_ANTHROPIC_API_KEY", "SONDER_ANTHROPIC_API_KEY_2", "SONDER_LIVE_ANTHROPIC"):
        assert name.startswith("SONDER_")
        assert name in conftest._LIVE_PROVIDER_VARIABLES
    import os

    assert "SONDER_ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_API_KEY" not in os.environ


# -- streaming -----------------------------------------------------------------------


def test_stream_yields_deltas_then_a_final_chunk_and_records_usage(ledger):
    sdk = FakeSdk(lambda k, p: _message(text="hello world", input_tokens=10, output_tokens=4))
    gateway = _gateway(sdk, ledger)
    chunks = list(gateway.stream(_request(), _context()))
    assert "".join(chunk.text for chunk in chunks) == "hello world"
    final = chunks[-1]
    assert (final.text, final.finish_reason, final.input_tokens, final.output_tokens) == (
        "", "end_turn", 10, 4)
    assert sdk.calls[0]["stream"] is True
    assert ledger.snapshot()["calls"] == 1


def test_stream_fails_over_on_a_connect_time_401_but_not_on_a_server_error(ledger):
    sdk = FakeSdk(_fail_key_one(FakeApiError(401, "bad key")))
    chunks = list(_gateway(sdk, ledger).stream(_request(), _context()))
    assert sdk.keys_used == [KEY_1, KEY_2]
    assert "".join(c.text for c in chunks) == "hello from claude"
    sdk = FakeSdk(_fail_key_one(FakeApiError(500, "boom")))
    with pytest.raises(DependencyUnavailable):
        list(_gateway(sdk, ledger).stream(_request(), _context()))
    assert sdk.keys_used == [KEY_1]


# -- wiring ----------------------------------------------------------------------------


def test_tier_binding_routes_to_the_gateway_and_only_when_bound(monkeypatch):
    assert provider_id_for_label("anthropic") == "anthropic"
    assert PROVIDER_LABEL_IDS["anthropic"] == "anthropic"
    assert provider_bindings_from_env({}).bound_providers == frozenset({"ollama"})
    env = {"SONDER_REASONING_PROVIDER": " Anthropic "}
    assert provider_bindings_from_env(env).tier_providers["reasoning"] == "anthropic"
    assert provider_bindings_from_env({"SONDER_MODEL_BACKEND": "anthropic"}).bound_providers == frozenset({"anthropic"})
    # A uniform binding builds the real gateway directly...
    assert isinstance(build_model_gateway(ProviderBindings.uniform("anthropic")), AnthropicGateway)

    # ...and a mixed binding dispatches exactly the bound tier to it.
    class Marker:
        def __init__(self, name):
            self.name, self.seen = name, []

        def generate(self, request, context):
            self.seen.append(request.tier)
            return ModelResponse(text=self.name, model=self.name, tier=request.tier)

        def embed(self, texts, context):
            return ()

    claude, local = Marker("claude"), Marker("local")
    bindings = ProviderBindings(
        default_generation_provider="ollama",
        tier_providers={"fast": "ollama", "general": "ollama", "code": "ollama",
                        "reasoning": "anthropic", "vision": "ollama"},
        embedding_provider="ollama",
    )
    gateway = build_model_gateway(bindings, {"ollama": lambda: local, "anthropic": lambda: claude})
    assert isinstance(gateway, ProviderDispatchGateway)
    assert gateway.generate(ModelRequest(prompt="p", tier="reasoning"), _context()).text == "claude"
    assert gateway.generate(ModelRequest(prompt="p", tier="fast"), _context()).text == "local"
    assert claude.seen == ["reasoning"] and local.seen == ["fast"]


def test_the_hosted_data_boundary_and_telemetry_know_the_provider():
    from sonder_runtime.application.chat import provider_bridge
    from sonder_runtime.application.observability.runtime_telemetry import PROVIDER_IDS

    assert provider_bridge.is_hosted("anthropic")
    assert PROVIDER_IDS["anthropic"] == "anthropic"


def test_credit_exhaustion_rule_matches_only_the_documented_message():
    rule = anthropic_policy.is_credit_exhausted
    assert rule(400, CREDIT_MESSAGE)
    assert rule(402, "anything")
    assert not rule(400, "messages: roles must alternate")
    assert not rule(400, "credit card declined")
    assert not rule(500, CREDIT_MESSAGE)
    assert not rule(401, CREDIT_MESSAGE)


# -- endpoint and credential hijack through the SDK's own environment reading ------------------

HOSTILE_ENV = {
    "ANTHROPIC_BASE_URL": "http://attacker.invalid:9",
    "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: attacker-key\nX-Evil: 1",
    "ANTHROPIC_AUTH_TOKEN": "attacker-token",
    "ANTHROPIC_API_KEY": "attacker-api-key",
}


def test_real_sdk_client_ignores_every_anthropic_environment_variable(ledger, monkeypatch):
    anthropic = pytest.importorskip("anthropic", reason="the optional anthropic SDK is not installed")
    for name, value in HOSTILE_ENV.items():
        monkeypatch.setenv(name, value)
    settings = _settings()
    gateway = AnthropicGateway(
        settings, spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
    )
    client = gateway._client(1, KEY_1, settings)
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
    assert client.api_key == KEY_1 and client.auth_token is None
    headers = {name.lower(): value for name, value in client.default_headers.items()}
    assert headers["x-api-key"] == KEY_1
    assert "x-evil" not in headers and "authorization" not in headers
    assert "attacker" not in json.dumps(headers, default=str)
    assert client.max_retries == 0
    # The request the SDK would actually put on the wire.
    request = client._build_request(anthropic._base_client.FinalRequestOptions.construct(
        method="post", url="/v1/messages", json_data={}))
    assert request.url.host == "api.anthropic.com" and request.url.scheme == "https"
    assert request.headers["x-api-key"] == KEY_1
    assert "x-evil" not in request.headers
    # A configured override is honoured (it is validated to be https).
    proxied = _settings(base_url="https://proxy.example.test/claude")
    other = AnthropicGateway(
        proxied, spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
    )._client(1, KEY_1, proxied)
    assert str(other.base_url).startswith("https://proxy.example.test/claude")
    gateway.close()


def test_the_sdk_environment_is_scrubbed_in_the_test_process():
    import os

    for name in HOSTILE_ENV:
        assert name not in os.environ
    import conftest

    assert set(HOSTILE_ENV) <= set(conftest._ANTHROPIC_SDK_VARIABLES)
    assert "SONDER_ANTHROPIC_BASE_URL" in conftest._LIVE_PROVIDER_VARIABLES


@pytest.mark.parametrize("value", [
    "http://api.anthropic.com", "https://user:pw@api.anthropic.com", "https://a.test/?x=1",
    "https://a.test/#f", "ftp://a.test", "api.anthropic.com", "https://",
])
def test_base_url_override_must_be_plain_https(value):
    with pytest.raises(InvalidInput):
        config_from_env({"SONDER_ANTHROPIC_BASE_URL": value})
    assert config_from_env({}).base_url == "https://api.anthropic.com"
    assert config_from_env(
        {"SONDER_ANTHROPIC_BASE_URL": "https://a.test:8443/x/"}).base_url == "https://a.test:8443/x"


def test_work_narration_knows_the_anthropic_base_url_variable():
    from sonder_runtime.adapters.observability.work_narration_facts import _ENV_BY_PROVIDER

    assert _ENV_BY_PROVIDER["anthropic"] == "SONDER_ANTHROPIC_BASE_URL"


# -- unfinished streams still bill what they consumed -----------------------------------------


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _partial_cost():
    # opus 5.5: 100 input + 7 output tokens
    return anthropic_policy.usage_cost_usd("claude-opus-5-5", input_tokens=100, output_tokens=7)


def _capture_reservations(ledger, monkeypatch):
    """Record every worst-case amount the gateway reserves on ``ledger``."""
    amounts = []
    original = ledger.try_reserve

    def try_reserve(amount, cap):
        amounts.append(amount)
        return original(amount, cap)

    monkeypatch.setattr(ledger, "try_reserve", try_reserve)
    return amounts


def test_an_abandoned_stream_is_charged_its_worst_case_not_the_stale_snapshot(ledger, monkeypatch):
    reserved = _capture_reservations(ledger, monkeypatch)
    gate = threading.Event()
    sdk = FakeSdk(lambda k, p: _message(
        text="hello world", input_tokens=100, output_tokens=7, plan={"gate": gate}))
    gateway = _gateway(sdk, ledger)
    stream = gateway.stream(_request(), _context())
    assert next(stream).text == "hello"
    stream.close()          # the consumer walks away mid-stream
    gate.set()              # the SDK stream then yields again and finds the stop flag
    assert _wait_for(lambda: ledger.snapshot()["calls"] == 1)
    (worst,) = reserved
    assert worst > _partial_cost()  # the snapshot's output count would understate it
    assert Decimal(ledger.snapshot()["usd"]) == worst
    assert ledger.snapshot()["calls"] == 1  # recorded exactly once


@pytest.mark.parametrize("error, expected", [
    (FakeApiError(500, "stream broke"), DependencyUnavailable),
    (APITimeoutError("read timeout"), DeadlineExceeded),
], ids=["mid_stream_error", "timeout_after_message_start"])
def test_a_stream_that_fails_after_message_start_is_charged_its_worst_case(
        ledger, monkeypatch, error, expected):
    reserved = _capture_reservations(ledger, monkeypatch)
    sdk = FakeSdk(lambda k, p: _message(
        text="hello world", input_tokens=100, output_tokens=7, plan={"fail": error}))
    with pytest.raises(expected):
        list(_gateway(sdk, ledger).stream(_request(), _context()))
    assert ledger.snapshot()["calls"] == 1
    (worst,) = reserved
    assert Decimal(ledger.snapshot()["usd"]) == worst > _partial_cost()
    assert ledger._inflight == {}


def test_an_unfinished_stream_whose_snapshot_exceeds_the_worst_case_keeps_the_snapshot(ledger):
    error = APITimeoutError("read timeout")
    sdk = FakeSdk(lambda k, p: _message(
        text="hello world", input_tokens=500_000, output_tokens=7, plan={"fail": error}))
    with pytest.raises(DeadlineExceeded):
        list(_gateway(sdk, ledger, _settings(daily_usd=Decimal(100))).stream(
            _request(), _context()))
    expected = anthropic_policy.usage_cost_usd(
        "claude-opus-5-5", input_tokens=500_000, output_tokens=7)
    assert Decimal(ledger.snapshot()["usd"]) == expected


# -- non-streaming calls that fail after the request was sent -------------------------------


@pytest.mark.parametrize("error", [
    APITimeoutError("read timeout"), ConnectionResetError("dropped"), TimeoutError("slow"),
], ids=["sdk_timeout", "connection_reset", "socket_timeout"])
def test_a_generate_that_fails_after_send_is_charged_its_worst_case(ledger, monkeypatch, error):
    reserved = _capture_reservations(ledger, monkeypatch)
    sdk = FakeSdk(lambda k, p: (_ for _ in ()).throw(error))
    with pytest.raises((DeadlineExceeded, DependencyUnavailable)):
        _gateway(sdk, ledger).generate(_request(options={"num_predict": 1000}), _context())
    (worst,) = reserved
    assert worst > 0
    assert ledger.snapshot()["calls"] == 1
    assert Decimal(ledger.snapshot()["usd"]) == worst
    assert ledger._inflight == {}  # the reservation became spend, it is not double-held


def test_repeated_timeouts_reach_the_daily_cap(ledger):
    sdk = FakeSdk(lambda k, p: (_ for _ in ()).throw(APITimeoutError("t")))
    # opus 5.5 worst case for max_tokens 1000 is about $0.0223: two fit a $0.05 cap.
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.05")))
    for _ in range(2):
        with pytest.raises(DeadlineExceeded):
            gateway.generate(_request(options={"num_predict": 1000}), _context())
    with pytest.raises(AnthropicDailyCapReached):
        gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert len(sdk.calls) == 2


class ConnectError(Exception):
    """Same class name as httpx2's connect-phase error."""


class APIConnectionError(Exception):
    """Same class name as the SDK's connection error; no status code."""


def _wrapped_connect_error():
    try:
        raise ConnectError("refused")
    except ConnectError as cause:
        error = APIConnectionError("Connection error.")
        error.__cause__ = cause
        return error


@pytest.mark.parametrize("make_error", [
    lambda: ConnectionRefusedError("refused"), _wrapped_connect_error,
], ids=["refused", "sdk_wrapped_connect_error"])
def test_a_generate_whose_connection_never_opened_is_not_charged(ledger, make_error):
    sdk = FakeSdk(lambda k, p: (_ for _ in ()).throw(make_error()))
    with pytest.raises(DependencyUnavailable):
        _gateway(sdk, ledger).generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 0
    assert ledger._inflight == {}


def test_a_client_that_cannot_be_built_is_not_charged(ledger):
    def factory(key, timeout):
        raise APITimeoutError("t")

    gateway = AnthropicGateway(
        _settings(), client_factory=factory, spend_ledger=ledger,
        request_admission=HostModelRequestAdmission(),
    )
    with pytest.raises(DeadlineExceeded):
        gateway.generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 0


def test_real_sdk_transport_errors_are_charged_only_after_send(ledger):
    anthropic = pytest.importorskip("anthropic", reason="the optional anthropic SDK is not installed")
    httpx = pytest.importorskip("httpx2", reason="httpx2 comes with the anthropic SDK")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def raising(sdk_error, cause):
        def behave(key, params):
            raise sdk_error from cause
        return behave

    read_timeout = raising(anthropic.APITimeoutError(request=request),
                           httpx.ReadTimeout("read", request=request))
    with pytest.raises(DeadlineExceeded):
        _gateway(FakeSdk(read_timeout), ledger).generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 1
    for cause in (httpx.ConnectError("refused", request=request),
                  httpx.ConnectTimeout("connect", request=request),
                  httpx.PoolTimeout("pool", request=request)):
        error_type = (anthropic.APITimeoutError if isinstance(cause, httpx.TimeoutException)
                      else anthropic.APIConnectionError)
        sdk_error = error_type(request=request)
        with pytest.raises((DeadlineExceeded, DependencyUnavailable)):
            _gateway(FakeSdk(raising(sdk_error, cause)), ledger).generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 1  # none of the never-sent failures was charged


@pytest.mark.parametrize("make_error", [
    lambda: APITimeoutError("t"), lambda: ConnectionResetError("dropped"),
], ids=["sdk_timeout", "connection_reset"])
def test_a_stream_that_fails_before_message_start_is_charged_its_worst_case(ledger, monkeypatch, make_error):
    reserved = _capture_reservations(ledger, monkeypatch)
    sdk = FakeSdk(lambda k, p: _message(plan={"fail": make_error(), "before_start": True}))
    with pytest.raises((DeadlineExceeded, DependencyUnavailable)):
        list(_gateway(sdk, ledger).stream(_request(options={"num_predict": 1000}), _context()))
    (worst,) = reserved
    assert ledger.snapshot()["calls"] == 1
    assert Decimal(ledger.snapshot()["usd"]) == worst
    assert ledger._inflight == {}


def test_a_stream_whose_connection_never_opened_is_not_charged(ledger):
    sdk = FakeSdk(lambda k, p: _message(plan={"fail": _wrapped_connect_error(), "before_start": True}))
    with pytest.raises(DependencyUnavailable):
        list(_gateway(sdk, ledger).stream(_request(), _context()))
    assert ledger.snapshot()["calls"] == 0
    assert ledger._inflight == {}


def test_repeated_stream_timeouts_reach_the_daily_cap(ledger):
    sdk = FakeSdk(lambda k, p: _message(plan={"fail": APITimeoutError("t"), "before_start": True}))
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.05")))
    for _ in range(2):
        with pytest.raises(DeadlineExceeded):
            list(gateway.stream(_request(options={"num_predict": 1000}), _context()))
    with pytest.raises(AnthropicDailyCapReached):
        list(gateway.stream(_request(options={"num_predict": 1000}), _context()))
    assert len(sdk.calls) == 2


def test_a_completed_stream_is_recorded_once_not_twice(ledger):
    sdk = FakeSdk(lambda k, p: _message(text="hello world", input_tokens=100, output_tokens=7))
    list(_gateway(sdk, ledger).stream(_request(), _context()))
    assert ledger.snapshot()["calls"] == 1
    assert Decimal(ledger.snapshot()["usd"]) == _partial_cost()


# -- no failover once a chunk has reached the consumer ---------------------------------------------


@pytest.mark.parametrize("error", [
    FakeApiError(401, "expired mid-stream"), FakeApiError(400, CREDIT_MESSAGE),
], ids=["401", "credit"])
def test_no_failover_after_a_chunk_was_delivered(ledger, error):
    def behave(key, params):
        plan = {"fail": error} if key == KEY_1 else None
        return _message(text="hello world", plan=plan)

    sdk = FakeSdk(behave)
    received = []
    with pytest.raises((Forbidden, AnthropicCreditsExhausted)):
        for chunk in _gateway(sdk, ledger).stream(_request(), _context()):
            received.append(chunk.text)
    assert received == ["hello"]       # the consumer already has text from key 1
    assert sdk.keys_used == [KEY_1]    # key 2 is never tried: it would replay billed work


# -- error chain carries no SDK exception -------------------------------------------------------------


def _chain(error):
    seen = []
    while error is not None and error not in seen:
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


def test_mapped_errors_have_no_cause_or_context_with_the_fake_sdk(ledger):
    class Leaky(FakeApiError):
        def __init__(self):
            super().__init__(500, "boom")
            self.response.request = SimpleNamespace(headers={"x-api-key": KEY_1})

    for factory_error in (Leaky(), FakeApiError(401, "bad"), APITimeoutError("t")):
        sdk = FakeSdk(lambda k, p, e=factory_error: (_ for _ in ()).throw(e))
        with pytest.raises(SonderError) as caught:
            _gateway(sdk, ledger).generate(_request(), _context())
        assert _chain(caught.value) == [caught.value]
    with pytest.raises(SonderError) as caught:
        list(_gateway(FakeSdk(lambda k, p: (_ for _ in ()).throw(Leaky())), ledger).stream(
            _request(), _context()))
    assert _chain(caught.value) == [caught.value]


def test_mapped_errors_have_no_sdk_exception_in_their_chain_with_the_real_sdk(ledger):
    anthropic = pytest.importorskip("anthropic", reason="the optional anthropic SDK is not installed")
    httpx = pytest.importorskip("httpx2", reason="httpx2 comes with the anthropic SDK")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages",
                            headers={"x-api-key": KEY_1})
    response = httpx.Response(401, request=request, json={"error": {"message": "bad key"}})

    def behave(key, params):
        raise anthropic.AuthenticationError("bad key", response=response, body=None)

    with pytest.raises(Forbidden) as caught:
        _gateway(FakeSdk(behave), ledger, _settings(api_key_2="")).generate(_request(), _context())
    assert _chain(caught.value) == [caught.value]
    assert KEY_1 not in "".join(traceback.format_exception(caught.value))


# -- concurrency: in-flight calls count against the cap --------------------------------------------------


def test_concurrent_calls_near_the_cap_cannot_both_be_admitted(ledger):
    entered, release = threading.Event(), threading.Event()

    def behave(key, params):
        entered.set()
        release.wait(5)
        return _message()

    sdk = FakeSdk(behave)
    # opus 5.5 worst case for max_tokens 1000 is about $0.0223: one call fits under
    # a $0.03 cap, but its reservation leaves no room for a second.
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.03")))
    outcome = {}

    def first():
        outcome["first"] = gateway.generate(_request(options={"num_predict": 1000}), _context())

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(5)
    with pytest.raises(AnthropicDailyCapReached, match="in flight"):
        gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert len(sdk.calls) == 1
    release.set()
    worker.join(5)
    assert outcome["first"].text == "hello from claude"
    # The reservation is gone and the real (tiny) cost counts: the next call is fine.
    assert ledger._inflight == {}
    gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert len(sdk.calls) == 2


def test_reservation_is_released_after_a_failed_call(ledger):
    sdk = FakeSdk(lambda k, p: (_ for _ in ()).throw(FakeApiError(500, "x")))
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.03")))
    for _ in range(3):
        with pytest.raises(DependencyUnavailable):
            gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert len(sdk.calls) == 3
    assert ledger._inflight == {}


# -- the cap counts the call being admitted --------------------------------------------------------------


def test_try_reserve_refuses_a_call_whose_worst_case_would_pass_the_cap(ledger):
    ledger.record(usd=Decimal("4.99"), slot=1, input_tokens=1, output_tokens=1)
    token, spent, inflight = ledger.try_reserve(Decimal(3), Decimal(5))
    assert token is None and spent == Decimal("4.99") and inflight == Decimal(0)
    assert ledger._inflight == {}
    token, spent, inflight = ledger.try_reserve(Decimal("0.01"), Decimal(5))  # exact fit
    assert token is not None and spent == Decimal("4.99")
    assert set(ledger._inflight) == {token}
    assert ledger._inflight[token][2] == Decimal("0.01")
    # In-flight reservations count too: nothing more fits once the cap is exactly full.
    assert ledger.try_reserve(Decimal("0.0001"), Decimal(5))[0] is None
    ledger.release(token)
    assert ledger.try_reserve(Decimal("0.01"), Decimal(5))[0] is not None


def test_try_reserve_counts_inflight_plus_amount_against_the_cap(ledger):
    first, _, _ = ledger.try_reserve(Decimal(2), Decimal(5))
    assert first is not None
    assert ledger.try_reserve(Decimal("3.01"), Decimal(5))[0] is None
    assert ledger.try_reserve(Decimal(3), Decimal(5))[0] is not None  # 2 + 3 == cap


def test_a_call_whose_worst_case_exceeds_the_whole_cap_is_refused_with_that_reason(ledger):
    sdk = FakeSdk()
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.01")))
    with pytest.raises(AnthropicDailyCapReached) as caught:
        gateway.generate(_request(options={"num_predict": 1000}), _context())  # worst ~ $0.0223
    assert sdk.calls == [] and ledger._inflight == {}
    message = str(caught.value)
    assert "larger than the whole $0.0100 daily cap" in message
    assert "cap reached" not in message and "SONDER_ANTHROPIC_DAILY_USD" in message
    assert caught.value.spent_usd == Decimal(0)


def test_a_call_that_would_pass_the_cap_names_its_worst_case_in_the_refusal(ledger):
    sdk = FakeSdk()
    ledger.record(usd=Decimal("0.02"), slot=1, input_tokens=1, output_tokens=1)
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.03")))
    with pytest.raises(AnthropicDailyCapReached) as caught:
        gateway.generate(_request(options={"num_predict": 1000}), _context())  # $0.02 + ~$0.0223
    assert sdk.calls == []
    message = str(caught.value)
    assert "$0.0200 spent today" in message and "worst-case cost $0.0223" in message
    assert "would take the day over the $0.0300 cap" in message


def test_a_response_without_usage_is_charged_the_reserved_worst_case(ledger, caplog):
    def no_usage(key, params):
        message = _message()
        message.usage = None
        return message

    sdk = FakeSdk(no_usage)
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal(1)))
    with caplog.at_level(logging.WARNING):
        gateway.generate(_request(options={"num_predict": 1000}), _context())
    spent = ledger.today_usd()
    assert Decimal("0.02") < spent < Decimal("0.03")  # the reservation (~$0.0223), not zero
    assert "worst-case cost was charged" in caplog.text
    assert ledger._inflight == {}
    streamed = _gateway(FakeSdk(no_usage), ledger, _settings(daily_usd=Decimal(1)))
    list(streamed.stream(_request(options={"num_predict": 1000}), _context()))
    assert ledger.today_usd() > spent * 2 - Decimal("0.001")


def test_a_zero_cap_says_anthropic_is_disabled(ledger):
    sdk = FakeSdk()
    with pytest.raises(AnthropicDailyCapReached) as caught:
        _gateway(sdk, ledger, _settings(daily_usd=Decimal(0))).generate(_request(), _context())
    assert "disabled" in str(caught.value) and "shorten" not in str(caught.value)
    assert sdk.calls == []


@pytest.mark.parametrize("usage", [SimpleNamespace(), SimpleNamespace(
    input_tokens=None, output_tokens=None, cache_read_input_tokens=5)])
def test_a_usage_object_with_no_token_counts_is_charged_the_reserved_worst_case(ledger, usage):
    def empty(key, params):
        message = _message()
        message.usage = usage
        return message

    gateway = _gateway(FakeSdk(empty), ledger, _settings(daily_usd=Decimal(1)))
    gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert Decimal("0.02") < ledger.today_usd() < Decimal("0.03")


def test_a_response_with_partial_usage_keeps_per_field_defaults(ledger):
    def partial(key, params):
        message = _message()
        message.usage = SimpleNamespace(input_tokens=1_000, output_tokens=None)
        return message

    gateway = _gateway(FakeSdk(partial), ledger, _settings(daily_usd=Decimal(1)))
    gateway.generate(_request(options={"num_predict": 1000}), _context())
    assert ledger.today_usd() == Decimal("0.004")  # 1000 input tokens only


# -- pricing multiplier and client reuse --------------------------------------------------------------------


def test_us_only_inference_is_priced_at_the_documented_multiplier(ledger):
    sdk = FakeSdk(lambda k, p: _message(input_tokens=1_000, output_tokens=1_000, inference_geo="us"))
    gateway = _gateway(sdk, ledger)
    gateway.generate(_request(), _context())
    assert gateway.last_usage["cost_usd"] == Decimal("0.0264")  # 0.024 * 1.1
    sdk = FakeSdk(lambda k, p: _message(input_tokens=1_000, output_tokens=1_000, inference_geo="global"))
    other = _gateway(sdk, SpendLedger(ledger.path.with_name("other.json")))
    other.generate(_request(), _context())
    assert other.last_usage["cost_usd"] == Decimal("0.024")


def test_one_sdk_client_is_reused_per_key_slot_and_closed_on_close(ledger):
    sdk = FakeSdk(_fail_key_one(FakeApiError(401, "bad")))
    gateway = _gateway(sdk, ledger)
    for _ in range(3):
        gateway.generate(_request(), _context())
    assert len(sdk.builds) == 2           # one client for slot 1, one for slot 2
    gateway.close()
    assert sdk.closed == 2
    gateway.generate(_request(), _context())
    assert len(sdk.builds) == 4           # rebuilt lazily after close
    assert sdk.call_timeouts[0] is not None  # the per-call deadline rides on the request


def test_a_changed_key_rebuilds_that_slots_client(ledger):
    sdk = FakeSdk()
    env = {"SONDER_ALLOW_CLOUD": "1", "SONDER_ANTHROPIC_API_KEY": KEY_1}
    gateway = AnthropicGateway(env=env, client_factory=sdk.factory, spend_ledger=ledger,
                               request_admission=HostModelRequestAdmission())
    gateway.generate(_request(), _context())
    gateway.generate(_request(), _context())
    assert len(sdk.builds) == 1
    env["SONDER_ANTHROPIC_API_KEY"] = KEY_2
    gateway.generate(_request(), _context())
    assert len(sdk.builds) == 2 and sdk.keys_used[-1] == KEY_2
    assert sdk.closed == 1


# -- the 429 cool-down is per key slot ------------------------------------------------------------------------


def test_a_429_on_key_two_does_not_cool_down_key_one_or_trigger_failover(ledger):
    clock = [10.0]
    state = {"key1_ok": False}

    def behave(key, params):
        if key == KEY_1 and not state["key1_ok"]:
            raise FakeApiError(401, "bad")
        if key == KEY_2:
            raise FakeApiError(429, "slow", {"retry-after": "30"})
        return _message()

    sdk = FakeSdk(behave)
    gateway = _gateway(sdk, ledger, monotonic=lambda: clock[0])
    with pytest.raises(AnthropicRateLimited):      # key 1 -> 401 -> key 2 -> 429
        gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1, KEY_2]
    assert set(gateway._cooldown_until) == {2}
    # Key 1 is refused again; failover into the cooled-down key 2 is not attempted.
    with pytest.raises(Forbidden):
        gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1, KEY_2, KEY_1]
    state["key1_ok"] = True
    assert gateway.generate(_request(), _context()).text == "hello from claude"  # key 1 not blocked


def test_a_429_on_key_one_blocks_new_calls_with_a_message_naming_the_slot(ledger):
    clock = [10.0]
    sdk = FakeSdk(lambda k, p: (_ for _ in ()).throw(FakeApiError(429, "s", {"retry-after": "30"})))
    gateway = _gateway(sdk, ledger, monotonic=lambda: clock[0])
    with pytest.raises(AnthropicRateLimited):
        gateway.generate(_request(), _context())
    with pytest.raises(AnthropicRateLimited, match="key slot 1"):
        gateway.generate(_request(), _context())
    assert sdk.keys_used == [KEY_1]


# -- stream-path reservation release, SDK logging pin, client verification ----------------------------


def _near_cap_gateway(sdk, ledger):
    # opus 5.5 worst case for max_tokens 1000 is about $0.0223: one reservation fits a
    # $0.03 cap but leaves no room for a second while it is held.
    return _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.03")))


def _admitted_after(gateway, sdk):
    before = len(sdk.calls)
    gateway.generate(_request(options={"num_predict": 1000}), _context())
    return len(sdk.calls) == before + 1


def test_a_completed_stream_releases_its_reservation(ledger):
    sdk = FakeSdk(lambda k, p: _message(text="hello world"))
    gateway = _near_cap_gateway(sdk, ledger)
    list(gateway.stream(_request(options={"num_predict": 1000}), _context()))
    assert ledger._inflight == {}
    assert _admitted_after(gateway, sdk)


def test_a_stream_that_fails_midway_releases_its_reservation(ledger):
    sdk = FakeSdk(lambda k, p: _message(
        text="hello world", plan={"fail": FakeApiError(500, "stream broke")}))
    # The unfinished stream is charged its worst case (~$0.0223).  A $0.05 cap then
    # admits one more such call, but not while the failed call's reservation is held.
    gateway = _gateway(sdk, ledger, _settings(daily_usd=Decimal("0.05")))
    with pytest.raises(DependencyUnavailable):
        list(gateway.stream(_request(options={"num_predict": 1000}), _context()))
    assert ledger._inflight == {}
    sdk.behave = lambda k, p: _message()
    assert _admitted_after(gateway, sdk)


def _fake_sdk_module(monkeypatch, **client_attrs):
    class Anthropic:
        def __init__(self, **kwargs):
            self.api_key, self.auth_token = kwargs["api_key"], None
            self.base_url = kwargs["base_url"] + "/"
            self._custom_headers = {}
            self.messages = SimpleNamespace(create=lambda **params: _message())
            for name, value in client_attrs.items():
                setattr(self, name, value)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Anthropic))


def test_a_client_that_carries_an_auth_token_is_refused_before_any_send(ledger, monkeypatch):
    _fake_sdk_module(monkeypatch, auth_token="token-from-environment")
    gateway = AnthropicGateway(
        _settings(), spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
    )
    with pytest.raises(DependencyUnavailable, match="did not take the configured endpoint"):
        gateway.generate(_request(), _context())
    assert ledger.snapshot()["calls"] == 0


def test_a_client_with_the_wrong_base_url_or_key_is_refused(ledger, monkeypatch):
    for attrs in ({"base_url": "http://attacker.invalid:9/"}, {"api_key": "someone-elses-key"}):
        _fake_sdk_module(monkeypatch, **attrs)
        gateway = AnthropicGateway(
            _settings(), spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
        )
        with pytest.raises(DependencyUnavailable, match="did not take"):
            gateway.generate(_request(), _context())


def test_sdk_loggers_are_pinned_to_warning_whatever_anthropic_log_says(ledger, monkeypatch):
    names = ("anthropic", "anthropic._base_client", "httpx2")
    saved = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.DEBUG)  # what ANTHROPIC_LOG=debug does
        monkeypatch.setenv("ANTHROPIC_LOG", "debug")
        _fake_sdk_module(monkeypatch)
        gateway = AnthropicGateway(
            _settings(), spend_ledger=ledger, request_admission=HostModelRequestAdmission(),
        )
        gateway.generate(_request(), _context())
        for name in names:
            assert logging.getLogger(name).level == logging.WARNING
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
