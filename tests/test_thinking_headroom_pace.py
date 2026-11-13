"""A local model is asked only for what its measured speed can deliver inside the timeout.

Seen live on 2026-11-13: a local model writing 4.5 tokens a second was given the 4096 token
headroom under a 300 second timeout (about 1,300 tokens fit) and the call failed with a read
timeout.  With the headroom cut back it spent the whole request thinking and needed a second
one (561 seconds in all), so a model too slow to write the reply and a minimum of thought
inside the timeout is now asked once, without thinking.  A model with time for both keeps
thinking, also under the 45 second timeout of a fan-out call.  These pin the pure rules, the
per-process speed cache and the way ``_chat_request`` uses them.
"""
import io
import urllib.error

import pytest

import server
from sonder_runtime.adapters import model_pace
from sonder_runtime.domain.thinking_pace import (
    MIN_FITTED_TOKENS,
    MIN_SAMPLE_TOKENS,
    MIN_THINKING_TOKENS,
    PACE_MARGIN,
    RATE_SMOOTHING,
    affordable_tokens,
    fit_headroom,
    output_rate,
    retry_with_room,
    smoothed_rate,
    without_thinking,
)

MINIMUM = server.LOCAL_THINKING_MIN_NUM_PREDICT
THINKING_ONLY = {"message": {"thinking": "long deliberation", "content": ""}, "done_reason": "length"}
SLOW = {"eval_count": 45, "eval_duration": 10_000_000_000}  # 4.5 tokens a second
FAST = {"eval_count": 500, "eval_duration": 10_000_000_000}  # 50 tokens a second
VERY_SLOW = {"eval_count": 16, "eval_duration": 100_000_000_000}  # 0.16 tokens a second
MEASURED_FAST = {"eval_count": 529, "eval_duration": 10_000_000_000}  # 52.9, the larger model live
FANOUT_TIMEOUT, FANOUT_NUM_PREDICT = 45, 512  # model_fanout's defaults


@pytest.fixture(autouse=True)
def _fresh_state():
    saved_capability = dict(server._THINKING_CAPABILITY_CACHE)
    saved_unsupported = set(server._THINK_OPTION_UNSUPPORTED_CACHE)
    server._THINKING_CAPABILITY_CACHE.clear()
    server._THINK_OPTION_UNSUPPORTED_CACHE.clear()
    server._model_pace.reset()
    yield
    server._model_pace.reset()
    server._THINKING_CAPABILITY_CACHE.clear()
    server._THINKING_CAPABILITY_CACHE.update(saved_capability)
    server._THINK_OPTION_UNSUPPORTED_CACHE.clear()
    server._THINK_OPTION_UNSUPPORTED_CACHE.update(saved_unsupported)


def _record(monkeypatch, reply):
    """Patch the transport; ``reply(payload, index)`` returns the response body or raises."""
    seen = []

    def fake_post_model(path, payload, **kwargs):
        seen.append((payload["options"].get("num_predict"), payload.get("think")))
        return reply(payload, len(seen)), 1

    monkeypatch.setattr(server, "_post_model", fake_post_model)
    return seen


def _ask(num_predict=900, **kwargs):
    return server._chat_request(
        {"model": "r", "messages": [{"role": "user", "content": "q"}],
         "options": {"num_predict": num_predict}},
        model="r", **kwargs,
    )


def _answer(**fields):
    return lambda _payload, _index: {"message": {"content": "an answer"}, **fields}


def _affordable(timing, timeout=None):
    return int(output_rate(timing) * (timeout or server.TIMEOUT) * PACE_MARGIN)


def _payload(num_predict):
    return {"model": "m", "messages": [], "options": {"num_predict": num_predict, "temperature": 0.2}}


# --- thinking_pace: output_rate ---------------------------------------------------------------

def test_output_rate_is_tokens_per_second_from_ollama_timing():
    assert output_rate({"eval_count": 45, "eval_duration": 10_000_000_000}) == 4.5
    assert output_rate({"eval_count": 100, "eval_duration": 2_000_000_000}) == 50.0


def test_output_rate_accepts_exactly_the_minimum_sample():
    assert MIN_SAMPLE_TOKENS == 16
    assert output_rate({"eval_count": 16, "eval_duration": 1_000_000_000}) == 16.0
    assert output_rate({"eval_count": 15, "eval_duration": 1_000_000_000}) is None


@pytest.mark.parametrize("response", [
    {},
    {"eval_count": 100},
    {"eval_duration": 1_000_000_000},
    {"eval_count": 0, "eval_duration": 1_000_000_000},
    {"eval_count": -50, "eval_duration": 1_000_000_000},
    {"eval_count": 100, "eval_duration": 0},
    {"eval_count": 100, "eval_duration": -1},
    {"eval_count": True, "eval_duration": 1_000_000_000},
    {"eval_count": 100, "eval_duration": True},
    {"eval_count": 100.0, "eval_duration": 1_000_000_000},
    {"eval_count": "100", "eval_duration": 1_000_000_000},
    {"eval_count": None, "eval_duration": None},
])
def test_output_rate_is_none_when_the_figures_are_missing_or_unusable(response):
    assert output_rate(response) is None


@pytest.mark.parametrize("response", [None, "text", 5, [("eval_count", 100)], ["eval_count"]])
def test_output_rate_is_none_for_a_response_that_is_not_a_mapping(response):
    assert output_rate(response) is None


# --- thinking_pace: affordable_tokens ---------------------------------------------------------

def test_affordable_tokens_is_rate_times_timeout_times_margin():
    assert PACE_MARGIN == 0.8
    assert affordable_tokens(4.5, 300) == int(4.5 * 300 * 0.8) == 1080
    assert affordable_tokens(10, 100) == 800
    assert affordable_tokens(10, 100, margin=1.0) == 1000


def test_affordable_tokens_truncates_toward_zero():
    assert affordable_tokens(1.0, 1.1) == 0  # 0.88 tokens
    assert affordable_tokens(3, 1) == 2  # 2.4 tokens


@pytest.mark.parametrize("rate, timeout", [
    (None, 300), (0, 300), (-1.0, 300), (True, 300), ("4.5", 300),
    (4.5, None), (4.5, 0), (4.5, -10), (4.5, True), (4.5, "300"),
])
def test_affordable_tokens_is_none_without_two_positive_numbers(rate, timeout):
    assert affordable_tokens(rate, timeout) is None


# --- thinking_pace: fit_headroom --------------------------------------------------------------

def test_fit_headroom_cuts_the_headroom_to_what_is_affordable():
    payload, caller = _payload(MINIMUM), _payload(900)

    fitted = fit_headroom(payload, caller, 1500)

    assert fitted["options"]["num_predict"] == 1500
    assert fitted["options"]["temperature"] == 0.2
    assert fitted["model"] == "m"


def test_fit_headroom_never_goes_below_the_callers_budget():
    fitted = fit_headroom(_payload(MINIMUM), _payload(2000), 1080)

    assert fitted["options"]["num_predict"] == 2000


def test_fit_headroom_with_zero_affordable_keeps_the_callers_budget():
    assert fit_headroom(_payload(MINIMUM), _payload(900), 0)["options"]["num_predict"] == 900


def test_fit_headroom_returns_the_same_payload_when_nothing_is_known():
    payload = _payload(MINIMUM)

    assert fit_headroom(payload, _payload(900), None) is payload


@pytest.mark.parametrize("affordable", [MINIMUM, MINIMUM + 1, 100_000])
def test_fit_headroom_returns_the_same_payload_when_the_headroom_is_affordable(affordable):
    payload = _payload(MINIMUM)

    assert fit_headroom(payload, _payload(900), affordable) is payload


@pytest.mark.parametrize("budget, asked", [(900, 900), (900, 2000)])
def test_fit_headroom_leaves_a_payload_with_no_headroom_over_the_caller(budget, asked):
    payload = _payload(budget)

    assert fit_headroom(payload, _payload(asked), 10) is payload


@pytest.mark.parametrize("bad_payload", [
    {"model": "m"},
    {"model": "m", "options": {}},
    {"model": "m", "options": None},
    {"model": "m", "options": {"num_predict": "4096"}},
    {"model": "m", "options": {"num_predict": True}},
    {"model": "m", "options": {"num_predict": 4096.0}},
    {"model": "m", "options": {"num_predict": 0}},
])
def test_fit_headroom_leaves_a_payload_without_a_usable_budget_alone(bad_payload):
    assert fit_headroom(bad_payload, _payload(900), 10) is bad_payload
    assert fit_headroom(_payload(MINIMUM), bad_payload, 10)["options"]["num_predict"] == MINIMUM


def test_fit_headroom_leaves_a_payload_alone_when_the_caller_payload_is_not_a_payload():
    payload = _payload(MINIMUM)

    assert fit_headroom(payload, None, 10) is payload
    assert fit_headroom(payload, "x", 10) is payload


def test_fit_headroom_does_not_mutate_its_inputs_or_share_options_with_the_copy():
    payload, caller = _payload(MINIMUM), _payload(900)

    fitted = fit_headroom(payload, caller, 1500)

    assert fitted is not payload
    assert fitted["options"] is not payload["options"]
    assert payload["options"]["num_predict"] == MINIMUM
    assert caller["options"]["num_predict"] == 900
    fitted["options"]["temperature"] = 9
    assert payload["options"]["temperature"] == 0.2


# --- model_pace -------------------------------------------------------------------------------

def test_model_pace_observe_then_rate():
    assert model_pace.rate("m") is None

    model_pace.observe("m", SLOW)

    assert model_pace.rate("m") == 4.5


def test_model_pace_does_not_record_a_hosted_call():
    model_pace.observe("m", FAST, local=False)

    assert model_pace.rate("m") is None


@pytest.mark.parametrize("unmeasurable", [
    {}, {"eval_count": 3, "eval_duration": 1_000_000_000}, {"eval_count": 100}, None, "x",
])
def test_model_pace_unmeasurable_response_keeps_the_earlier_measurement(unmeasurable):
    model_pace.observe("m", SLOW)

    model_pace.observe("m", unmeasurable)

    assert model_pace.rate("m") == 4.5


def test_model_pace_a_later_measurement_is_averaged_with_the_earlier_one():
    model_pace.observe("m", SLOW)
    model_pace.observe("m", FAST)

    assert model_pace.rate("m") == pytest.approx(27.25)


def test_model_pace_one_slow_reply_does_not_cost_a_fast_model_its_thinking():
    """One reply at a fifth of the usual speed (a busy machine) leaves the fan-out call thinking."""
    model_pace.observe("m", MEASURED_FAST)
    model_pace.observe("m", {"eval_count": 100, "eval_duration": 10_000_000_000})

    sent = model_pace.up_front(_payload(MINIMUM), _payload(FANOUT_NUM_PREDICT), "m", FANOUT_TIMEOUT, True)

    assert int(10.0 * FANOUT_TIMEOUT * PACE_MARGIN) < FANOUT_NUM_PREDICT + MIN_THINKING_TOKENS
    assert "think" not in sent
    assert sent["options"]["num_predict"] > FANOUT_NUM_PREDICT + MIN_THINKING_TOKENS


def test_smoothed_rate_takes_the_first_measurement_as_it_is():
    assert smoothed_rate(None, 4.5) == 4.5


def test_smoothed_rate_moves_part_of_the_way_to_a_new_measurement():
    assert 0 < RATE_SMOOTHING < 1
    assert smoothed_rate(10.0, 20.0) == pytest.approx(10.0 + 10.0 * RATE_SMOOTHING)
    assert smoothed_rate(20.0, 10.0) == pytest.approx(20.0 - 10.0 * RATE_SMOOTHING)
    assert smoothed_rate(10.0, 20.0, 0.25) == pytest.approx(12.5)


def test_model_pace_names_are_stripped_and_blank_names_are_ignored():
    model_pace.observe("  m \n", SLOW)

    assert model_pace.rate("m") == 4.5
    assert model_pace.rate(" m ") == 4.5
    model_pace.observe("   ", FAST)
    model_pace.observe(None, FAST)
    assert model_pace.rate("") is None
    assert model_pace.rate(None) is None


def test_model_pace_keeps_each_model_separate():
    model_pace.observe("slow", SLOW)
    model_pace.observe("fast", FAST)

    assert (model_pace.rate("slow"), model_pace.rate("fast")) == (4.5, 50.0)


def test_model_pace_reset_forgets_everything():
    model_pace.observe("m", SLOW)

    model_pace.reset()

    assert model_pace.rate("m") is None


def test_model_pace_fit_leaves_an_unmeasured_model_alone():
    payload = _payload(MINIMUM)

    assert model_pace.fit(payload, _payload(900), "m", 300) is payload


def test_model_pace_fit_cuts_a_measured_model_to_what_it_can_write():
    model_pace.observe("m", SLOW)

    fitted = model_pace.fit(_payload(MINIMUM), _payload(900), "m", 300)

    assert fitted["options"]["num_predict"] == 1080


# --- thinking_pace: without_thinking ----------------------------------------------------------

def test_without_thinking_asks_the_callers_request_with_thinking_off():
    caller = _payload(900)

    plain = without_thinking(_payload(MINIMUM), caller, 1080)

    assert plain == {**caller, "think": False}
    assert plain["options"]["num_predict"] == 900


def test_without_thinking_cuts_a_reply_the_timeout_cannot_deliver():
    assert without_thinking(_payload(MINIMUM), _payload(2000), 1080)["options"]["num_predict"] == 1080


@pytest.mark.parametrize("affordable", [0, 1, MIN_FITTED_TOKENS - 1])
def test_without_thinking_never_cuts_under_the_smallest_reply(affordable):
    plain = without_thinking(_payload(MINIMUM), _payload(900), affordable)

    assert plain["options"]["num_predict"] == MIN_FITTED_TOKENS


def test_without_thinking_keeps_a_callers_reply_smaller_than_the_smallest_fitted_one():
    assert without_thinking(_payload(MINIMUM), _payload(20), 5)["options"]["num_predict"] == 20


@pytest.mark.parametrize("affordable", [None, 900 + MIN_THINKING_TOKENS, MINIMUM, MINIMUM + 1, 100_000])
def test_without_thinking_is_none_when_the_model_has_time_to_think(affordable):
    assert without_thinking(_payload(MINIMUM), _payload(900), affordable) is None


def test_without_thinking_applies_one_token_short_of_the_reply_and_the_minimum_of_thought():
    plain = without_thinking(_payload(MINIMUM), _payload(900), 900 + MIN_THINKING_TOKENS - 1)

    assert (plain["options"]["num_predict"], plain["think"]) == (900, False)


def test_without_thinking_measures_against_the_reply_not_the_whole_headroom():
    """The rule this replaces switched thinking off whenever the 4096 headroom would not arrive."""
    affordable = FANOUT_NUM_PREDICT + MIN_THINKING_TOKENS

    assert affordable < MINIMUM
    assert without_thinking(_payload(MINIMUM), _payload(FANOUT_NUM_PREDICT), affordable) is None


@pytest.mark.parametrize("asked", [MINIMUM, MINIMUM + 1, 8192])
@pytest.mark.parametrize("affordable", [0, 1080, 7200])
def test_without_thinking_leaves_a_request_that_was_given_no_headroom(asked, affordable):
    """A caller who sized the budget at or above the headroom gets the request unchanged."""
    assert without_thinking(_payload(asked), _payload(asked), affordable) is None


@pytest.mark.parametrize("choice", [True, False, "high"])
def test_without_thinking_leaves_an_explicit_thinking_choice_alone(choice):
    assert without_thinking({**_payload(MINIMUM), "think": choice}, _payload(900), 1080) is None
    assert without_thinking(_payload(MINIMUM), {**_payload(900), "think": choice}, 1080) is None


@pytest.mark.parametrize("payload, caller", [
    ({"model": "m"}, {"model": "m", "options": {"num_predict": 900}}),
    ({"model": "m", "options": {"num_predict": 4096}}, {"model": "m"}),
    ({"model": "m", "options": {"num_predict": 4096}}, {"model": "m", "options": {"num_predict": 0}}),
])
def test_without_thinking_is_none_without_two_usable_budgets(payload, caller):
    assert without_thinking(payload, caller, 1080) is None


def test_without_thinking_does_not_mutate_or_share_with_its_inputs():
    payload, caller = _payload(MINIMUM), _payload(900)

    plain = without_thinking(payload, caller, 500)
    plain["options"]["temperature"] = 9

    assert payload == _payload(MINIMUM)
    assert caller == _payload(900)
    assert plain["options"]["num_predict"] == 500


# --- model_pace: up_front ---------------------------------------------------------------------

def test_model_pace_up_front_leaves_an_unmeasured_model_alone():
    payload = _payload(MINIMUM)

    assert model_pace.up_front(payload, _payload(900), "m", 300, True) is payload


def test_model_pace_up_front_asks_a_slow_model_without_thinking():
    model_pace.observe("m", SLOW)

    plain = model_pace.up_front(_payload(MINIMUM), _payload(900), "m", 300, True)

    assert (plain["options"]["num_predict"], plain["think"]) == (900, False)


def test_model_pace_up_front_fits_the_headroom_when_thinking_cannot_be_switched_off():
    model_pace.observe("m", SLOW)

    fitted = model_pace.up_front(_payload(MINIMUM), _payload(900), "m", 300, False)

    assert fitted["options"]["num_predict"] == 1080
    assert "think" not in fitted


def test_model_pace_up_front_leaves_a_fast_model_its_headroom():
    model_pace.observe("m", FAST)
    payload = _payload(MINIMUM)

    assert model_pace.up_front(payload, _payload(900), "m", 300, True) is payload


# --- _chat_request ----------------------------------------------------------------------------

def test_a_known_slow_thinking_model_is_asked_once_without_thinking(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())

    _out, content = _ask(900)

    assert _affordable(SLOW) < MINIMUM
    assert content == "an answer"
    assert seen == [(900, False)]


def test_a_slow_model_that_refused_the_thinking_switch_gets_the_fitted_headroom(monkeypatch):
    server._remember_thinking_model("r")
    server._remember_unsupported_think_option("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())

    _ask(900)

    fitted = max(900, _affordable(SLOW))
    assert 900 < fitted < MINIMUM
    assert [budget for budget, _think in seen] == [fitted]
    assert seen[0][1] is not False


def test_a_slow_model_keeps_thinking_when_the_caller_asked_for_it(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())

    server._chat_request(
        {"model": "r", "messages": [{"role": "user", "content": "q"}], "think": True,
         "options": {"num_predict": 900}},
        model="r",
    )

    assert seen == [(max(900, _affordable(SLOW)), True)]


def test_a_slow_model_asked_without_thinking_that_writes_nothing_is_not_asked_again(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, lambda _payload, _index: dict(THINKING_ONLY))

    with pytest.raises(server.ModelCallError) as raised:
        _ask(900)

    assert raised.value.kind == "empty_response"
    assert seen == [(900, False)]


def test_a_very_slow_model_is_asked_for_the_smallest_reply(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", VERY_SLOW)
    seen = _record(monkeypatch, _answer())

    _ask(260)

    assert _affordable(VERY_SLOW) < MIN_FITTED_TOKENS
    assert seen == [(MIN_FITTED_TOKENS, False)]


def test_a_known_fast_thinking_model_keeps_the_full_headroom(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", FAST)
    seen = _record(monkeypatch, _answer())

    _ask(900)

    assert _affordable(FAST) >= MINIMUM
    assert [budget for budget, _think in seen] == [MINIMUM]


def test_a_slow_model_is_sent_what_it_can_deliver_when_the_callers_budget_would_not_arrive(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())

    _ask(2000)

    assert _affordable(SLOW) < 2000
    assert seen == [(_affordable(SLOW), False)]


def test_an_explicit_timeout_changes_what_a_slow_model_is_asked_for(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())

    _ask(900, timeout=500)
    _ask(900, timeout=300)
    _ask(900, timeout=100)

    assert _affordable(SLOW, 500) >= 900 + MIN_THINKING_TOKENS > _affordable(SLOW, 300) > 900 > _affordable(SLOW, 100)
    assert seen == [(_affordable(SLOW, 500), None), (900, False), (_affordable(SLOW, 100), False)]


def test_a_fast_thinking_model_keeps_thinking_under_the_fanout_timeout(monkeypatch):
    """52.9 tokens a second cannot write 4096 tokens in 45 seconds; it can think and write 512."""
    server._remember_thinking_model("r")
    server._model_pace.observe("r", MEASURED_FAST)
    seen = _record(monkeypatch, _answer())

    _ask(FANOUT_NUM_PREDICT, timeout=FANOUT_TIMEOUT)

    fitted = _affordable(MEASURED_FAST, FANOUT_TIMEOUT)
    assert FANOUT_NUM_PREDICT + MIN_THINKING_TOKENS <= fitted < MINIMUM
    assert seen == [(fitted, None)]


@pytest.mark.parametrize("timing", [SLOW, FAST])
@pytest.mark.parametrize("asked", [MINIMUM, 8192])
def test_a_budget_at_or_above_the_headroom_is_sent_as_the_caller_sized_it(monkeypatch, timing, asked):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", timing)
    seen = _record(monkeypatch, _answer())

    _ask(asked)

    assert seen == [(asked, None)]


def _refusing_the_switch(monkeypatch, then):
    """Patch the wire: a request that carries ``think`` is refused, any other gets ``then``."""
    sent = []

    def fake_post(_path, payload, timeout=None):
        sent.append((payload["options"].get("num_predict"), payload.get("think", "absent")))
        if "think" in payload:
            raise urllib.error.HTTPError(server.BASE + "/api/chat", 400, "failure", {},
                                         io.BytesIO(b'{"error":"model does not support thinking"}'))
        return dict(then)

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(server, "_post", fake_post)
    return sent


def test_a_slow_model_that_refuses_the_switch_on_the_first_ask_still_answers(monkeypatch):
    """The transport drops the refused switch and asks again; later calls keep the fitted headroom."""
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    sent = _refusing_the_switch(monkeypatch, {"message": {"content": "an answer"}})

    _out, content = _ask(900)
    _ask(900)

    assert content == "an answer"
    assert not server._think_option_supported("r")
    assert sent == [(900, False), (900, "absent"), (max(900, _affordable(SLOW)), "absent")]


def test_a_slow_model_that_refuses_the_switch_and_only_thinks_fails_as_an_empty_response(monkeypatch):
    """Nothing more is sent for that call: a third request would outlast the caller's timeout."""
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    sent = _refusing_the_switch(monkeypatch, THINKING_ONLY)

    with pytest.raises(server.ModelCallError) as raised:
        _ask(900)

    assert raised.value.kind == "empty_response"
    assert sent == [(900, False), (900, "absent")]


def test_a_timeout_long_enough_for_the_headroom_leaves_it_whole(monkeypatch):
    server._remember_thinking_model("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, _answer())
    needed = int(MINIMUM / (4.5 * PACE_MARGIN)) + 1

    _ask(900, timeout=needed)

    assert [budget for budget, _think in seen] == [MINIMUM]


def test_an_unmeasured_known_thinking_model_gets_the_full_headroom(monkeypatch):
    server._remember_thinking_model("r")
    seen = _record(monkeypatch, _answer())

    _ask(900)

    assert [budget for budget, _think in seen] == [MINIMUM]


def test_a_slow_unknown_model_that_only_thought_is_asked_again_without_thinking(monkeypatch):
    """The first reply shows the model thinks; too slow for headroom, the second ask has none."""
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": "the real answer"}} if payload.get("think") is False
        else dict(THINKING_ONLY)))

    _out, content = _ask(900)

    assert 900 < _affordable(SLOW) < MINIMUM
    assert content == "the real answer"
    assert seen == [(900, None), (900, False)]


def test_a_slow_unknown_model_that_refused_the_switch_retries_with_the_fitted_headroom(monkeypatch):
    server._remember_unsupported_think_option("r")
    server._model_pace.observe("r", SLOW)
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": "the real answer"}} if payload["options"]["num_predict"] != 900
        else dict(THINKING_ONLY)))

    _out, content = _ask(900)

    fitted = max(900, _affordable(SLOW))
    assert 900 < fitted < MINIMUM
    assert content == "the real answer"
    assert [budget for budget, _think in seen] == [900, fitted]


def test_the_headroom_retry_of_a_fast_unknown_model_gets_the_full_headroom(monkeypatch):
    server._model_pace.observe("r", FAST)
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": "the real answer"}} if payload["options"]["num_predict"] != 900
        else dict(THINKING_ONLY)))

    _ask(900)

    assert [budget for budget, _think in seen] == [900, MINIMUM]


@pytest.mark.parametrize("reply_timing", [
    {"eval_count": 500, "eval_duration": 10_000_000_000},  # slower than held: the fitted figure drops
    {"eval_count": 700, "eval_duration": 10_000_000_000},  # faster than held: the fitted figure rises
])
def test_a_reply_that_used_its_fitted_headroom_on_thought_is_not_sent_again_with_headroom(monkeypatch, reply_timing):
    """The speed measured from that reply moves the fitted figure; that is not more room."""
    server._remember_thinking_model("r")
    server._model_pace.observe("r", MEASURED_FAST)
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": "the real answer"}} if payload.get("think") is False
        else {**THINKING_ONLY, **reply_timing}))

    _out, content = _ask(FANOUT_NUM_PREDICT, timeout=FANOUT_TIMEOUT)

    assert _affordable(reply_timing, FANOUT_TIMEOUT) != _affordable(MEASURED_FAST, FANOUT_TIMEOUT)
    assert content == "the real answer"
    assert seen == [(_affordable(MEASURED_FAST, FANOUT_TIMEOUT), None), (FANOUT_NUM_PREDICT, False)]


def test_retry_with_room_gives_an_unsized_request_the_affordable_headroom():
    payload, caller = _payload(900), _payload(900)

    assert retry_with_room(payload, _payload(MINIMUM), caller, 1080)["options"]["num_predict"] == 1080
    assert retry_with_room(payload, _payload(MINIMUM), caller, None)["options"]["num_predict"] == MINIMUM


@pytest.mark.parametrize("affordable", [0, 899, 900])
def test_retry_with_room_returns_the_request_itself_when_no_more_is_affordable(affordable):
    payload = _payload(900)

    assert retry_with_room(payload, _payload(MINIMUM), _payload(900), affordable) is payload


@pytest.mark.parametrize("affordable", [None, 1000, 1897, 2500, 100_000])
def test_retry_with_room_returns_a_request_that_already_carried_headroom(affordable):
    payload = _payload(1897)

    assert retry_with_room(payload, _payload(MINIMUM), _payload(512), affordable) is payload


def test_retry_with_room_returns_a_request_without_a_budget():
    payload = {"model": "m", "messages": []}

    assert retry_with_room(payload, dict(payload), payload, 1080) is payload


def test_no_headroom_retry_is_made_when_nothing_beyond_the_callers_budget_is_affordable(monkeypatch):
    server._model_pace.observe("r", VERY_SLOW)
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": "the real answer"}} if payload.get("think") is False
        else dict(THINKING_ONLY)))

    _out, content = _ask(260)

    assert _affordable(VERY_SLOW) <= 260
    assert content == "the real answer"
    assert seen == [(260, None), (260, False)]


def test_a_hosted_response_does_not_record_a_rate(monkeypatch):
    _record(monkeypatch, _answer(**FAST))

    server._chat_request(
        {"model": "custom:cloud", "messages": [], "options": {"num_predict": 900}},
        model="custom:cloud", cloud=True, idempotent=True,
    )

    assert server._model_pace.rate("custom:cloud") is None


def test_a_local_response_records_a_rate_that_fits_the_next_call(monkeypatch):
    server._remember_thinking_model("r")
    seen = _record(monkeypatch, _answer(**SLOW))

    _ask(900)
    assert server._model_pace.rate("r") == 4.5
    _ask(900)

    assert seen[0][0] == MINIMUM and seen[0][1] is not False
    assert seen[1] == (900, False)
