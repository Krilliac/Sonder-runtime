"""Spend cap on the OpenAI-compatible gateway: edge cases and guards.

Offline: every call goes through an injected transport and a tmp_path ledger
with a fake clock.  Expected dollar amounts are computed by hand from the
documented rule (tokens x USD-per-1M, rounded up to a micro-dollar), never by
calling the production cost function.
"""
from __future__ import annotations

import json
import math
import urllib.error
from decimal import Decimal

import pytest

from sonder_runtime.adapters.inference.anthropic_spend import (
    FILENAME,
    SCHEMA,
    SpendLedger,
    SpendLedgerUnreadable,
)
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    OpenAIDailyCapReached,
    OpenAISpendUnpriced,
    _config_from_env,
)
from sonder_runtime.adapters.inference.openrouter_gateway import (
    OpenRouterGateway,
    protocol_probe_gateway,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
)
from sonder_runtime.domain.openai_spend_policy import ChatPrice

REMOTE = "https://api.example.com"
DAY1 = 1_700_000_000.0  # 2023-11-14 UTC
DAY2 = DAY1 + 86_400.0
PRICE = ChatPrice(Decimal(1), Decimal(2))  # USD per 1M tokens
SECRET_PROMPT = "SECRET-PROMPT-XYZ"
SECRET_REPLY = "SECRET-REPLY-QRS"
SECRET_KEY = "sk-secret-key-123"


class _Clock:
    def __init__(self, now: float = DAY1) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Token:
    def __init__(self) -> None:
        self.cancelled = False

    def wait(self, timeout=None) -> bool:
        return self.cancelled


class _Abort(BaseException):
    """A cancellation-style exit that is not an Exception."""


def _ctx(**kw):
    kw.setdefault("cloud_allowed", True)
    kw.setdefault("timeout_seconds", 30.0)
    return local_owner_context(correlation_id="req_spend2", **kw)


def _ledger(tmp_path, clock=None, name="openai-spend.json"):
    return SpendLedger(
        tmp_path / name, clock=clock or _Clock(), filename="openai-spend.json",
        schema="sonder.openai_spend.v1", label="OpenAI-compatible",
    )


def _cfg(**kw):
    kw.setdefault("price", PRICE)
    kw.setdefault("api_key", SECRET_KEY)
    return OpenAICompatibleConfig(base_url=REMOTE, model="m", embed_model="e", **kw)


def _reply(prompt=1000, completion=500, text="hi"):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _req(prompt="hello", **options):
    return ModelRequest(prompt=prompt, tier="code", options=options or None)


class _Recorder:
    """Transport that records payloads and answers with ``response``."""

    def __init__(self, response=None):
        self.payloads: list[dict] = []
        self.response = response if response is not None else _reply()

    def __call__(self, url, payload, headers, timeout):
        self.payloads.append(payload)
        return self.response


def _gw(tmp_path, cfg, transport, clock=None):
    ledger = _ledger(tmp_path, clock)
    return OpenAICompatibleGateway(cfg, transport=transport, spend_ledger=ledger), ledger


def _prompt_bytes(prompt: str) -> int:
    """UTF-8 length of json.dumps(messages) as the gateway would send them."""
    rec = _Recorder()
    OpenAICompatibleGateway(
        _cfg(), transport=rec, spend_ledger=None,
    ).generate(_req(prompt), _ctx())
    return len(json.dumps(rec.payloads[0]["messages"]).encode("utf-8"))


def _micro(value: Decimal) -> Decimal:
    """Round up to a micro-dollar, written independently of the production code."""
    return Decimal(math.ceil(value)) / Decimal(1_000_000)


def _worst(prompt: str, out_tokens: int, rin=1, rout=2) -> Decimal:
    return _micro(Decimal(_prompt_bytes(prompt)) * rin + Decimal(out_tokens) * rout)


# -- SpendLedger generalization ------------------------------------------------


def test_spend_ledger_defaults_keep_the_anthropic_file_and_schema(tmp_path):
    ledger = SpendLedger(tmp_path / "a.json")
    assert (FILENAME, SCHEMA) == ("anthropic-spend.json", "sonder.anthropic_spend.v1")
    assert SpendLedger().path.name == "anthropic-spend.json"
    ledger.record(usd=Decimal("0.5"), slot=1, input_tokens=1, output_tokens=1)
    assert json.loads((tmp_path / "a.json").read_text("utf-8"))["schema"] == "sonder.anthropic_spend.v1"
    (tmp_path / "a.json").write_text("{not json", "utf-8")
    with pytest.raises(SpendLedgerUnreadable, match="Anthropic spend ledger"):
        ledger.today_usd()


def test_spend_ledger_custom_file_schema_and_label(tmp_path):
    ledger = _ledger(tmp_path)
    assert SpendLedger(filename="openai-spend.json").path.name == "openai-spend.json"
    ledger.record(usd=Decimal("0.5"), slot=1, input_tokens=1, output_tokens=1)
    assert json.loads((tmp_path / "openai-spend.json").read_text("utf-8"))["schema"] == "sonder.openai_spend.v1"
    (tmp_path / "openai-spend.json").write_text("{not json", "utf-8")
    with pytest.raises(SpendLedgerUnreadable, match="OpenAI-compatible spend ledger"):
        ledger.today_usd()


def test_default_gateway_ledger_is_the_openai_file():
    gw = OpenAICompatibleGateway(_cfg(), transport=_Recorder())
    assert gw._spend_ledger is not None
    assert gw._spend_ledger.path.name == "openai-spend.json"
    assert gw._spend_ledger.path.name != FILENAME


def test_spend_ledger_argument_is_type_checked():
    with pytest.raises(TypeError):
        OpenAICompatibleGateway(_cfg(), spend_ledger="not a ledger")  # type: ignore[arg-type]


# -- when metering applies ---------------------------------------------------


@pytest.mark.parametrize("base", ["http://127.0.0.1:8080", "http://localhost:8080"])
def test_loopback_is_never_metered_and_payload_is_unchanged(tmp_path, base):
    rec = _Recorder()
    ledger = _ledger(tmp_path)
    gw = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url=base, model="m"), transport=rec, spend_ledger=ledger,
    )
    gw.generate(_req(), _ctx(cloud_allowed=False))
    gw.generate(_req(num_predict=99_999), _ctx(cloud_allowed=False))  # above any ceiling
    assert "max_tokens" not in rec.payloads[0]
    assert rec.payloads[1]["max_tokens"] == 99_999  # not clamped
    assert not (tmp_path / "openai-spend.json").exists()
    assert ledger.today_usd() == 0


def test_unmetered_remote_payload_is_exactly_the_old_one(tmp_path):
    rec = _Recorder()
    gw = OpenAICompatibleGateway(_cfg(price=None), transport=rec, spend_ledger=None)
    gw.generate(_req(), _ctx())
    gw.generate(_req(num_predict=99_999), _ctx())
    assert rec.payloads[0] == {
        "model": "m",
        "messages": rec.payloads[0]["messages"],
        "stream": False,
    }
    assert rec.payloads[1]["max_tokens"] == 99_999
    assert gw.spend_snapshot() is None


# -- unpriced ------------------------------------------------------------------


def test_unpriced_generate_and_embed_are_refused_before_any_transport_call(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(price=None, embed_price=None), rec)
    with pytest.raises(OpenAISpendUnpriced) as chat:
        gw.generate(_req(), _ctx())
    assert isinstance(chat.value, Forbidden)
    assert "SONDER_OPENAI_PRICE_USD_PER_MTOK" in str(chat.value)
    assert '"0,0"' in str(chat.value)
    with pytest.raises(OpenAISpendUnpriced, match="SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK"):
        gw.embed(["x"], _ctx())
    assert rec.payloads == []
    assert not (tmp_path / "openai-spend.json").exists()


def test_priced_chat_does_not_make_embeddings_priced(tmp_path):
    rec = _Recorder()
    gw, _ = _gw(tmp_path, _cfg(embed_price=None), rec)
    with pytest.raises(OpenAISpendUnpriced):
        gw.embed(["x"], _ctx())
    assert rec.payloads == []


def test_zero_zero_price_is_admitted_and_records_zero_dollar_calls(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(price=ChatPrice(Decimal(0), Decimal(0)),
                                    embed_price=Decimal(0)), rec)
    gw.generate(_req(), _ctx())
    rec.response = {"data": [{"index": 0, "embedding": [0.5]}], "usage": {"prompt_tokens": 7}}
    gw.embed(["x"], _ctx())
    snap = ledger.snapshot()
    assert snap["calls"] == 2
    assert Decimal(snap["usd"]) == 0
    assert len(rec.payloads) == 2


# -- max_tokens ----------------------------------------------------------------


def test_max_tokens_default_ceiling_and_clamp(tmp_path):
    rec = _Recorder()
    gw, _ = _gw(tmp_path, _cfg(), rec)
    assert _cfg().max_tokens_ceiling == 16000 and _cfg().daily_usd == Decimal(10)
    gw.generate(_req(), _ctx())                       # absent -> ceiling
    gw.generate(_req(num_predict=16000), _ctx())      # equal -> unchanged
    gw.generate(_req(num_predict=16001), _ctx())      # one over -> clamped
    gw.generate(_req(num_predict=5), _ctx())          # below -> caller's value
    assert [p["max_tokens"] for p in rec.payloads] == [16000, 16000, 16000, 5]


# -- accounting ----------------------------------------------------------------


def test_actual_cost_is_charged_from_usage_rounded_up(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(price=ChatPrice(Decimal("0.15"), Decimal("0.6"))),
                     _Recorder(_reply(1234, 567)))
    gw.generate(_req(), _ctx())
    # 1234*0.15 + 567*0.6 = 185.1 + 340.2 = 525.3 micro-dollars -> 526 (total rounded up once).
    assert ledger.today_usd() == Decimal("0.000526")
    snap = ledger.snapshot()
    assert snap["calls"] == 1 and snap["key_slot_calls"] == {"1": 1, "2": 0}


def test_timings_fallback_counts_are_charged(tmp_path):
    response = {
        "choices": [{"message": {"content": "hi"}}],
        "timings": {"prompt_n": 2000, "predicted_n": 1000},
    }
    gw, ledger = _gw(tmp_path, _cfg(), _Recorder(response))
    gw.generate(_req(), _ctx())
    assert ledger.today_usd() == Decimal("0.004000")  # 2000*1 + 1000*2


def test_missing_usage_charges_the_worst_case(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000),
                     _Recorder({"choices": [{"message": {"content": "hi"}}]}))
    gw.generate(_req(), _ctx())
    assert ledger.today_usd() == _worst("hello", 1000)
    assert ledger.snapshot()["calls"] == 1


def test_partial_usage_charges_the_missing_part_at_worst_case(tmp_path):
    response = {"choices": [{"message": {"content": "hi"}}], "usage": {"completion_tokens": 10}}
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), _Recorder(response))
    gw.generate(_req(), _ctx())
    expected = _micro(Decimal(_prompt_bytes("hello")) * 1 + Decimal(10) * 2)
    assert ledger.today_usd() == expected
    # And the other way round: the prompt count is reported, the output count is not.
    other = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 10}}
    before = ledger.today_usd()
    gw2, ledger2 = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), _Recorder(other))
    gw2.generate(_req(), _ctx())
    assert ledger2.today_usd() - before == _micro(Decimal(10) * 1 + Decimal(1000) * 2)


@pytest.mark.parametrize("bad_usage", ["oops", [1, 2]])
def test_invalid_usage_object_charges_the_worst_case(tmp_path, bad_usage):
    response = {"choices": [{"message": {"content": "hi"}}], "usage": bad_usage}
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), _Recorder(response))
    with pytest.raises(DependencyUnavailable):
        gw.generate(_req(), _ctx())
    assert ledger.today_usd() == _worst("hello", 1000)


@pytest.mark.parametrize("code", [400, 401, 429, 500, 503])
def test_http_error_answer_is_not_charged_and_reservation_is_released(tmp_path, code):
    def transport(*a):
        raise urllib.error.HTTPError("", code, "err", {}, None)

    gw, ledger = _gw(tmp_path, _cfg(), transport)
    with pytest.raises((InvalidInput, Forbidden, CapacityExceeded, DependencyUnavailable)):
        gw.generate(_req(), _ctx())
    assert ledger.today_usd() == 0
    assert ledger.snapshot()["calls"] == 0
    token, _spent, inflight = ledger.try_reserve(Decimal(0), Decimal(1))
    assert token is not None and inflight == 0


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("slow"), DeadlineExceeded),
    (ConnectionResetError("reset"), DependencyUnavailable),
    (urllib.error.URLError(ConnectionResetError("reset")), DependencyUnavailable),
])
def test_failure_after_send_charges_the_worst_case(tmp_path, exc, expected):
    def transport(*a):
        raise exc

    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), transport)
    with pytest.raises(expected):
        gw.generate(_req(), _ctx())
    assert ledger.today_usd() == _worst("hello", 1000)
    assert ledger.snapshot()["calls"] == 1
    assert ledger.try_reserve(Decimal(0), Decimal(1))[2] == 0  # released


def test_non_exception_abort_after_send_charges_the_worst_case(tmp_path):
    def transport(*a):
        raise _Abort()

    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), transport)
    with pytest.raises(_Abort):
        gw.generate(_req(), _ctx())
    assert ledger.today_usd() == _worst("hello", 1000)
    assert ledger.try_reserve(Decimal(0), Decimal(1))[2] == 0


def test_non_object_body_after_send_charges_the_worst_case(tmp_path):
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), lambda *a: ["not", "a", "dict"])
    with pytest.raises(InternalFailure):
        gw.generate(_req(), _ctx())
    assert ledger.today_usd() == _worst("hello", 1000)


def test_failures_before_send_charge_nothing(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(), rec)
    with pytest.raises(Forbidden):  # no cloud consent
        gw.generate(_req(), _ctx(cloud_allowed=False))
    with pytest.raises(InvalidInput):  # empty prompt
        gw.generate(ModelRequest(prompt="  ", tier="code"), _ctx())
    cancelled = _Token()
    cancelled.cancelled = True
    with pytest.raises(Cancelled):  # cancelled before the send
        gw.generate(_req(), _ctx(cancellation=cancelled))
    plain = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="http://api.example.com", model="m", price=PRICE),
        transport=rec, spend_ledger=ledger,
    )
    with pytest.raises(Forbidden):  # plaintext http to a public host
        plain.generate(_req(), _ctx())
    assert rec.payloads == [] and ledger.today_usd() == 0
    assert ledger.snapshot()["calls"] == 0


def test_completed_response_is_recorded_even_if_the_caller_was_cancelled(tmp_path):
    token = _Token()

    def transport(url, payload, headers, timeout):
        token.cancelled = True  # cancelled while the provider was answering
        return _reply(1000, 500)

    gw, ledger = _gw(tmp_path, _cfg(), transport)
    with pytest.raises(Cancelled):
        gw.generate(_req(), _ctx(cancellation=token))
    assert ledger.today_usd() == Decimal("0.002000")
    assert ledger.snapshot()["calls"] == 1


def test_ledger_and_logs_are_content_free(tmp_path, caplog):
    gw, ledger = _gw(tmp_path, _cfg(),
                     _Recorder(_reply(10, 5, text=SECRET_REPLY)))
    with caplog.at_level("DEBUG"):
        gw.generate(_req(SECRET_PROMPT), _ctx())
    stored = (tmp_path / "openai-spend.json").read_text("utf-8")
    for secret in (SECRET_PROMPT, SECRET_REPLY, SECRET_KEY):
        assert secret not in stored
        assert secret not in caplog.text
    assert set(gw.spend_snapshot()) == {"day_utc", "usd", "calls", "key_slot_calls"}


# -- cap -----------------------------------------------------------------------


def test_cap_zero_refuses_before_any_transport_call(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(daily_usd=Decimal(0)), rec)
    with pytest.raises(OpenAIDailyCapReached, match="disabled") as err:
        gw.generate(_req(), _ctx())
    assert isinstance(err.value, Forbidden)
    assert err.value.cap_usd == 0 and err.value.spent_usd == 0
    # A free endpoint (worst case $0) is refused by cap 0 too.
    free, _ = _gw(tmp_path, _cfg(daily_usd=Decimal(0), price=ChatPrice(Decimal(0), Decimal(0))), rec)
    with pytest.raises(OpenAIDailyCapReached):
        free.generate(_req(), _ctx())
    assert rec.payloads == [] and ledger.today_usd() == 0


def test_single_call_larger_than_the_whole_cap_is_refused(tmp_path):
    rec = _Recorder()
    gw, _ = _gw(tmp_path, _cfg(daily_usd=Decimal("0.01")), rec)  # worst case ~ $0.032
    with pytest.raises(OpenAIDailyCapReached, match="larger than the whole") as err:
        gw.generate(_req(), _ctx())
    assert err.value.cap_usd == Decimal("0.01")
    assert rec.payloads == []


def test_cap_boundary_exact_fit_is_admitted_one_micro_less_is_refused(tmp_path):
    worst = _worst("hello", 1000)
    rec = _Recorder()
    fit, _ = _gw(tmp_path, _cfg(max_tokens_ceiling=1000, daily_usd=worst), rec)
    fit.generate(_req(), _ctx())
    assert len(rec.payloads) == 1
    tight, _ = _gw(tmp_path, _cfg(max_tokens_ceiling=1000, daily_usd=worst - Decimal("0.000001")),
                   rec, )
    with pytest.raises(OpenAIDailyCapReached):
        tight.generate(_req(), _ctx())
    assert len(rec.payloads) == 1


def test_worst_case_input_grows_with_the_request_size(tmp_path):
    prompt = "é" * 400  # a larger request has a larger worst case
    expected = _worst(prompt, 1000)
    rec = _Recorder({"choices": [{"message": {"content": "hi"}}]})  # no usage -> worst case
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), rec)
    gw.generate(_req(prompt), _ctx())
    assert ledger.today_usd() == expected
    assert expected > _worst("hello", 1000)


def test_day_over_the_cap_is_refused_and_resumes_on_the_next_utc_day(tmp_path):
    clock = _Clock(DAY1)
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000, daily_usd=Decimal("0.031")), rec, clock)
    ledger.record(usd=Decimal("0.030"), slot=1, input_tokens=0, output_tokens=0)
    with pytest.raises(OpenAIDailyCapReached, match="resume after 00:00 UTC") as err:
        gw.generate(_req(), _ctx())
    assert err.value.spent_usd == Decimal("0.03") and err.value.cap_usd == Decimal("0.031")
    assert rec.payloads == []
    clock.now = DAY2
    gw.generate(_req(), _ctx())
    assert len(rec.payloads) == 1


def test_concurrent_reservations_are_counted(tmp_path):
    worst = _worst("hello", 1000)
    cap = worst + worst / 2  # room for one call in flight, not two
    results: dict[str, object] = {}
    state: dict[str, OpenAICompatibleGateway] = {}

    def transport(url, payload, headers, timeout):
        # First call is still in flight: a second one must see its reservation.
        try:
            state["gw"].generate(_req(), _ctx())
        except OpenAIDailyCapReached as exc:
            results["second"] = exc
        return _reply(10, 5)

    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000, daily_usd=cap), transport)
    state["gw"] = gw
    gw.generate(_req(), _ctx())
    second = results["second"]
    assert isinstance(second, OpenAIDailyCapReached)
    assert "in flight" in str(second)
    assert ledger.snapshot()["calls"] == 1  # the refused second call was never sent or charged
    # Once the first finished and released, a call fits again.
    gw2_rec = _Recorder()
    again = OpenAICompatibleGateway(_cfg(max_tokens_ceiling=1000, daily_usd=cap),
                                    transport=gw2_rec, spend_ledger=ledger)
    again.generate(_req(), _ctx())
    assert len(gw2_rec.payloads) == 1


def test_spend_persists_across_gateway_instances_on_the_same_file(tmp_path):
    clock = _Clock()
    first, ledger = _gw(tmp_path, _cfg(), _Recorder(_reply(1000, 500)), clock)
    first.generate(_req(), _ctx())
    cap = Decimal("0.0021")  # fits one real call ($0.002) but not a second worst case
    rec = _Recorder()
    second = OpenAICompatibleGateway(
        _cfg(max_tokens_ceiling=50, daily_usd=Decimal("0.0021")), transport=rec,
        spend_ledger=_ledger(tmp_path, clock),  # a fresh ledger object, same file
    )
    assert second.spend_snapshot()["calls"] == 1
    assert Decimal(second.spend_snapshot()["usd"]) == Decimal("0.002000")
    with pytest.raises(OpenAIDailyCapReached, match="spent today"):
        second.generate(_req(), _ctx())
    assert rec.payloads == [] and cap == Decimal("0.0021")


# -- embeddings ----------------------------------------------------------------


def test_embed_is_charged_from_usage_and_missing_usage_charges_the_worst_case(tmp_path):
    vec = {"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 1500}}
    rec = _Recorder(vec)
    gw, ledger = _gw(tmp_path, _cfg(embed_price=Decimal("0.02")), rec)
    gw.embed(["hello"], _ctx())
    assert ledger.today_usd() == Decimal("0.000030")  # 1500 * 0.02 = 30 micro-dollars
    rec.response = {"data": [{"index": 0, "embedding": [0.1]}]}
    gw.embed(["héllo"], _ctx())  # 6 UTF-8 bytes at $0.02/M -> 0.12 -> 1 micro-dollar
    assert ledger.today_usd() == Decimal("0.000031")
    assert ledger.snapshot()["calls"] == 2


def test_embed_http_error_not_charged_but_timeout_charges_worst_case(tmp_path):
    def http_error(*a):
        raise urllib.error.HTTPError("", 500, "err", {}, None)

    gw, ledger = _gw(tmp_path, _cfg(embed_price=Decimal(1)), http_error)
    with pytest.raises(DependencyUnavailable):
        gw.embed(["hello"], _ctx())
    assert ledger.today_usd() == 0

    def timeout(*a):
        raise TimeoutError("slow")

    gw2, ledger2 = _gw(tmp_path, _cfg(embed_price=Decimal(1)), timeout)
    with pytest.raises(DeadlineExceeded):
        gw2.embed(["hello"], _ctx())
    assert ledger2.today_usd() == Decimal("0.000005")  # 5 bytes at $1/M
    assert ledger2.try_reserve(Decimal(0), Decimal(1))[2] == 0


def test_embed_cap_refusal_sends_nothing(tmp_path):
    rec = _Recorder({"data": [], "usage": {"prompt_tokens": 1}})
    gw, ledger = _gw(tmp_path, _cfg(embed_price=Decimal(1), daily_usd=Decimal(0)), rec)
    with pytest.raises(OpenAIDailyCapReached):
        gw.embed(["hello"], _ctx())
    big, _ = _gw(tmp_path, _cfg(embed_price=Decimal(10_000), daily_usd=Decimal("0.01")), rec)
    with pytest.raises(OpenAIDailyCapReached, match="larger than the whole"):
        big.embed(["hello"], _ctx())  # 5 bytes * $10000/M = $0.05 > $0.01
    assert rec.payloads == [] and ledger.today_usd() == 0


def test_embed_loopback_is_not_metered(tmp_path):
    rec = _Recorder({"data": [{"index": 0, "embedding": [0.1]}]})
    ledger = _ledger(tmp_path)
    gw = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:8080", model="m", embed_model="e"),
        transport=rec, spend_ledger=ledger,
    )
    gw.embed(["hello"], _ctx(cloud_allowed=False))
    assert not (tmp_path / "openai-spend.json").exists()


# -- environment ---------------------------------------------------------------


def _env(monkeypatch, **values):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", REMOTE)
    for name in ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK",
                 "SONDER_OPENAI_DAILY_USD", "SONDER_OPENAI_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_env_defaults(monkeypatch):
    _env(monkeypatch)
    cfg = _config_from_env()
    assert cfg.price is None and cfg.embed_price is None
    assert cfg.daily_usd == Decimal(10) and cfg.max_tokens_ceiling == 16000


@pytest.mark.parametrize("name,value", [
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "1"),
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "1,2,3"),
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "x,2"),
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "-1,2"),
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "1,10001"),
    ("SONDER_OPENAI_PRICE_USD_PER_MTOK", "NaN,1"),
    ("SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK", "1,2"),
    ("SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK", "-0.1"),
    ("SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK", "inf"),
    ("SONDER_OPENAI_DAILY_USD", "-1"),
    ("SONDER_OPENAI_DAILY_USD", "1000001"),
    ("SONDER_OPENAI_DAILY_USD", "abc"),
    ("SONDER_OPENAI_DAILY_USD", "nan"),
    ("SONDER_OPENAI_MAX_TOKENS", "0"),
    ("SONDER_OPENAI_MAX_TOKENS", "1000001"),
    ("SONDER_OPENAI_MAX_TOKENS", "1.5"),
    ("SONDER_OPENAI_MAX_TOKENS", "abc"),
])
def test_bad_env_values_raise_invalid_input_naming_the_variable(monkeypatch, name, value):
    _env(monkeypatch, **{name: value})
    with pytest.raises(InvalidInput, match=name):
        _config_from_env()


def test_env_boundaries_are_accepted(monkeypatch):
    _env(monkeypatch, SONDER_OPENAI_PRICE_USD_PER_MTOK="0,10000",
         SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK="0", SONDER_OPENAI_DAILY_USD="1000000",
         SONDER_OPENAI_MAX_TOKENS="1")
    cfg = _config_from_env()
    assert cfg.price == ChatPrice(Decimal(0), Decimal(10000))
    assert cfg.embed_price == Decimal(0) and cfg.daily_usd == Decimal(1_000_000)
    assert cfg.max_tokens_ceiling == 1
    _env(monkeypatch, SONDER_OPENAI_DAILY_USD="0", SONDER_OPENAI_MAX_TOKENS="1000000")
    assert _config_from_env().daily_usd == 0 and _config_from_env().max_tokens_ceiling == 1_000_000


def test_gateway_resolves_env_lazily_at_call_time(tmp_path, monkeypatch):
    _env(monkeypatch)
    rec = _Recorder()
    ledger = _ledger(tmp_path)
    gw = OpenAICompatibleGateway(transport=rec, spend_ledger=ledger)  # built before any env price
    with pytest.raises(OpenAISpendUnpriced, match="SONDER_OPENAI_PRICE_USD_PER_MTOK"):
        gw.generate(_req(), _ctx())
    monkeypatch.setenv("SONDER_OPENAI_PRICE_USD_PER_MTOK", "1,2")
    monkeypatch.setenv("SONDER_OPENAI_MAX_TOKENS", "321")
    gw.generate(_req(), _ctx())
    assert rec.payloads[0]["max_tokens"] == 321
    monkeypatch.setenv("SONDER_OPENAI_PRICE_USD_PER_MTOK", "oops")
    with pytest.raises(InvalidInput, match="SONDER_OPENAI_PRICE_USD_PER_MTOK"):
        gw.generate(_req(), _ctx())
    assert len(rec.payloads) == 1


def test_malformed_spend_env_never_breaks_loopback_or_unmetered(tmp_path, monkeypatch):
    _env(monkeypatch, SONDER_OPENAI_PRICE_USD_PER_MTOK="oops", SONDER_OPENAI_DAILY_USD="oops")
    rec = _Recorder()
    unmetered = OpenAICompatibleGateway(transport=rec, spend_ledger=None)
    unmetered.generate(_req(), _ctx())
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://127.0.0.1:8080")
    loop = OpenAICompatibleGateway(transport=rec, spend_ledger=_ledger(tmp_path))
    loop.generate(_req(), _ctx(cloud_allowed=False))
    assert len(rec.payloads) == 2


# -- other gateways stay unmetered ---------------------------------------------


def test_sonder_inference_gateway_is_unmetered():
    gw = SonderInferenceGateway()
    assert gw.spend_snapshot() is None
    assert gw._metered(OpenAICompatibleConfig(base_url=REMOTE, model="m")) is False


def test_openrouter_gateway_is_unmetered():
    gw = OpenRouterGateway()
    assert gw.spend_snapshot() is None
    assert gw._metered(OpenAICompatibleConfig(base_url="https://openrouter.ai/api", model="m")) is False


def test_protocol_probe_gateway_is_unmetered():
    gw = protocol_probe_gateway(
        "anthropic/claude-sonnet-4",
        env={"SONDER_ALLOW_CLOUD": "1", "OPENROUTER_API_KEY": SECRET_KEY},
        transport=_Recorder(),
    )
    assert gw.spend_snapshot() is None
    assert gw._metered(OpenAICompatibleConfig(base_url=REMOTE, model="m")) is False


def test_non_positive_num_predict_is_clamped_to_one_token(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=1000), rec)
    gw.generate(_req(num_predict=-1), _ctx())
    gw.generate(_req(num_predict=0), _ctx())
    assert [p["max_tokens"] for p in rec.payloads] == [1, 1]


def test_embed_unpriced_hint_declares_free_with_a_single_value(tmp_path):
    gw, _ledger_ = _gw(tmp_path, _cfg(), _Recorder())
    with pytest.raises(OpenAISpendUnpriced) as caught:
        gw.embed(["hello"], _ctx())
    assert '"0" declares a free endpoint' in str(caught.value)
    assert '"0,0"' not in str(caught.value)


def test_probe_style_chat_post_is_priced_reserved_and_recorded(tmp_path):
    rec = _Recorder(_reply(prompt=1000, completion=500))
    gw, ledger = _gw(tmp_path, _cfg(max_tokens_ceiling=64), rec)
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
               "stream": False, "temperature": 0.0}
    data = gw._post_chat_accounted(_cfg(max_tokens_ceiling=64), payload, 5.0,
                                   context=_ctx(), num_predict=128)
    assert data["choices"]
    assert rec.payloads[0]["max_tokens"] == 64  # clamped to the ceiling
    # 1000 in x 1 + 500 out x 2 = 2000 / 1e6 USD
    assert ledger.today_usd() == Decimal("0.002")
    assert ledger.snapshot()["calls"] == 1


def test_probe_style_chat_post_unpriced_is_refused_before_sending(tmp_path):
    rec = _Recorder()
    gw, ledger = _gw(tmp_path, _cfg(price=None), rec)
    with pytest.raises(OpenAISpendUnpriced):
        gw._post_chat_accounted(_cfg(price=None), {"model": "m", "messages": []}, 5.0,
                                context=_ctx(), num_predict=128)
    assert rec.payloads == [] and ledger.today_usd() == 0
