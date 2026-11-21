"""Spend cap for a metered (non-loopback) OpenAI-compatible endpoint.

Offline: every call goes through an injected transport and a tmp_path ledger.
"""
from __future__ import annotations

import json
import urllib.error
from decimal import Decimal

import pytest

from sonder_runtime.adapters.inference.anthropic_spend import SpendLedger
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    OpenAIDailyCapReached,
    OpenAISpendUnpriced,
    _config_from_env,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)
from sonder_runtime.domain.openai_spend_policy import (
    ChatPrice,
    OpenAISpendPolicyError,
    cost_usd,
    parse_chat_price,
    parse_embed_price,
)

REMOTE = "https://api.example.com"


def _ctx():
    return local_owner_context(correlation_id="req_spend", cloud_allowed=True, timeout_seconds=30.0)


def _ledger(tmp_path):
    return SpendLedger(
        tmp_path / "openai-spend.json", filename="openai-spend.json",
        schema="sonder.openai_spend.v1", label="OpenAI-compatible",
    )


def _cfg(**kw):
    kw.setdefault("price", ChatPrice(Decimal(1), Decimal(2)))
    return OpenAICompatibleConfig(base_url=REMOTE, model="m", embed_model="e", **kw)


def _ok(prompt=1000, completion=500):
    return {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _gw(tmp_path, cfg, transport):
    ledger = _ledger(tmp_path)
    return OpenAICompatibleGateway(cfg, transport=transport, spend_ledger=ledger), ledger


def _gen(gw):
    return gw.generate(ModelRequest(prompt="hello", tier="code"), _ctx())


def _usd(ledger):
    return ledger.today_usd()


def test_price_parsing_and_rounding():
    assert parse_chat_price("0.15, 0.60") == ChatPrice(Decimal("0.15"), Decimal("0.60"))
    assert parse_embed_price("0.02") == Decimal("0.02")
    for bad in ("1", "1,2,3", "a,b", "-1,2", "1,10001", "nan,1", "inf,1"):
        with pytest.raises(OpenAISpendPolicyError):
            parse_chat_price(bad)
    assert cost_usd(input_tokens=1, output_tokens=0, input_rate=Decimal("0.1")) == Decimal("0.000001")


def test_priced_call_is_charged_from_usage(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(), lambda *a: _ok(1000, 500))
    _gen(gw)
    assert _usd(ledger) == Decimal("0.002000")  # 1000*1 + 500*2 per 1M


def test_unpriced_is_refused_before_send(tmp_path):
    calls = []
    gw, ledger = _gw(tmp_path, _cfg(price=None), lambda *a: calls.append(a) or _ok())
    with pytest.raises(OpenAISpendUnpriced, match="SONDER_OPENAI_PRICE_USD_PER_MTOK"):
        _gen(gw)
    assert calls == [] and _usd(ledger) == 0


def test_free_endpoint_zero_zero_is_allowed(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(price=ChatPrice(Decimal(0), Decimal(0))), lambda *a: _ok())
    assert _gen(gw).text == "hi"
    assert _usd(ledger) == 0


def test_max_tokens_is_clamped_to_the_ceiling(tmp_path):
    seen = []
    gw, _ = _gw(tmp_path, _cfg(max_tokens_ceiling=100), lambda u, p, h, t: seen.append(p) or _ok())
    _gen(gw)
    gw.generate(ModelRequest(prompt="x", tier="code", options={"num_predict": 40}), _ctx())
    gw.generate(ModelRequest(prompt="x", tier="code", options={"num_predict": 4000}), _ctx())
    assert [p["max_tokens"] for p in seen] == [100, 40, 100]


def test_loopback_and_unmetered_are_unchanged(tmp_path):
    seen = []

    def transport(u, p, h, t):
        seen.append(p)
        return _ok()

    ledger = _ledger(tmp_path)
    local = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:8080", model="m"),
        transport=transport, spend_ledger=ledger,
    )
    local.generate(ModelRequest(prompt="x", tier="code"), _ctx())
    unmetered = OpenAICompatibleGateway(_cfg(price=None), transport=transport, spend_ledger=None)
    _gen(unmetered)
    assert all("max_tokens" not in p for p in seen)
    assert _usd(ledger) == 0


def test_cap_refusals(tmp_path):
    calls = []
    t = lambda *a: calls.append(a) or _ok()  # noqa: E731
    gw, _ = _gw(tmp_path, _cfg(daily_usd=Decimal(0)), t)
    with pytest.raises(OpenAIDailyCapReached, match="disabled") as zero:
        _gen(gw)
    assert zero.value.cap_usd == 0
    # worst case: 16000 output tokens * $2/M = $0.032 > a $0.01 cap.
    gw, _ = _gw(tmp_path, _cfg(daily_usd=Decimal("0.01")), t)
    with pytest.raises(OpenAIDailyCapReached, match="larger than the whole"):
        _gen(gw)
    assert calls == []


def test_day_over_the_cap_is_refused(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(daily_usd=Decimal("0.05")), lambda *a: _ok(1000, 500))
    ledger.record(usd=Decimal("0.03"), slot=1, input_tokens=0, output_tokens=0)
    with pytest.raises(OpenAIDailyCapReached, match="resume after 00:00 UTC") as err:
        _gen(gw)
    assert err.value.spent_usd == Decimal("0.03")


def test_missing_usage_charges_the_worst_case(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), lambda *a: {
        "choices": [{"message": {"content": "hi"}}]})
    _gen(gw)
    prompt_bytes = len(json.dumps([{"role": "user", "content": "hello"}]).encode())
    expected = cost_usd(input_tokens=prompt_bytes, output_tokens=1000,
                        input_rate=Decimal(1), output_rate=Decimal(2))
    assert _usd(ledger) == expected


def test_http_error_answer_is_not_charged(tmp_path):
    def transport(*a):
        raise urllib.error.HTTPError("", 429, "rate", {}, None)

    gw, ledger = _gw(tmp_path, _cfg(), transport)
    with pytest.raises(CapacityExceeded):
        _gen(gw)
    assert _usd(ledger) == 0


def test_timeout_after_send_charges_the_worst_case(tmp_path):
    def transport(*a):
        raise TimeoutError("slow")

    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), transport)
    with pytest.raises(DeadlineExceeded):
        _gen(gw)
    assert _usd(ledger) > 0


def test_invalid_body_after_send_charges_the_worst_case(tmp_path):
    def transport(*a):
        raise DependencyUnavailable("endpoint returned non-JSON")

    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), transport)
    with pytest.raises(DependencyUnavailable):
        _gen(gw)
    assert _usd(ledger) > 0


def test_failure_before_send_charges_nothing(tmp_path):
    calls = []
    gw, ledger = _gw(tmp_path, _cfg(), lambda *a: calls.append(a) or _ok())
    # Not cloud-consented: refused before anything is sent or reserved.
    with pytest.raises(Forbidden):
        gw.generate(ModelRequest(prompt="x", tier="code"),
                    local_owner_context(correlation_id="r", cloud_allowed=False, timeout_seconds=5.0))
    assert calls == [] and _usd(ledger) == 0


def test_reservation_is_released(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(), lambda *a: _ok())
    _gen(gw)
    token, _spent, inflight = ledger.try_reserve(Decimal(0), Decimal(100))
    assert token is not None and inflight == 0


def test_embed_priced_unpriced_and_missing_usage(tmp_path):
    vec = {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 1000}}
    gw, ledger = _gw(tmp_path, _cfg(embed_price=Decimal(1)), lambda *a: vec)
    assert len(gw.embed(["hello"], _ctx())) == 1
    assert _usd(ledger) == Decimal("0.001000")

    unpriced, _ = _gw(tmp_path, _cfg(), lambda *a: vec)
    with pytest.raises(OpenAISpendUnpriced, match="SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK"):
        unpriced.embed(["hello"], _ctx())

    no_usage = {"data": [{"index": 0, "embedding": [0.1]}]}
    gw3 = OpenAICompatibleGateway(
        _cfg(embed_price=Decimal(1)), transport=lambda *a: no_usage,
        spend_ledger=SpendLedger(tmp_path / "c.json"),
    )
    gw3.embed(["hello"], _ctx())
    assert gw3.spend_snapshot()["calls"] == 1
    assert Decimal(gw3.spend_snapshot()["usd"]) == Decimal("0.000005")  # 5 bytes at $1/M


def test_env_settings(monkeypatch):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", REMOTE)
    monkeypatch.setenv("SONDER_OPENAI_PRICE_USD_PER_MTOK", "0.15,0.6")
    monkeypatch.setenv("SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK", "0.02")
    monkeypatch.setenv("SONDER_OPENAI_DAILY_USD", "3")
    monkeypatch.setenv("SONDER_OPENAI_MAX_TOKENS", "500")
    cfg = _config_from_env()
    assert cfg.price == ChatPrice(Decimal("0.15"), Decimal("0.6"))
    assert cfg.embed_price == Decimal("0.02")
    assert cfg.daily_usd == Decimal(3) and cfg.max_tokens_ceiling == 500
    monkeypatch.setenv("SONDER_OPENAI_PRICE_USD_PER_MTOK", "oops")
    with pytest.raises(InvalidInput, match="SONDER_OPENAI_PRICE_USD_PER_MTOK"):
        _config_from_env()
    # An unmetered gateway or a loopback endpoint never parses the spend settings.
    assert _config_from_env(metered=False).price is None
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:8080")
    assert _config_from_env().price is None
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", REMOTE)
    monkeypatch.setenv("SONDER_OPENAI_PRICE_USD_PER_MTOK", "1,2")
    for name, value in (("SONDER_OPENAI_DAILY_USD", "-1"), ("SONDER_OPENAI_MAX_TOKENS", "0")):
        monkeypatch.setenv(name, value)
        with pytest.raises(InvalidInput, match=name):
            _config_from_env()
        monkeypatch.delenv(name)
