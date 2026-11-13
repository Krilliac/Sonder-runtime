"""A local reasoning model that spends its whole reply budget thinking still answers.

Observed live on 2026-11-13: ``astra-cyber-27b`` given a 420-word design-review
question with ``num_predict=900`` thought for 900 tokens, was given the 4096
headroom, thought for all 4096 (``thinking_chars=18800``, no content) and the
call failed with ``empty_response``.  The same question with thinking off was
answered in 11 seconds.  These pin the second ask: once headroom has not
produced an answer, the turn is asked once more with thinking off and the
caller's own reply budget.
"""
import pytest

import server

MINIMUM = server.LOCAL_THINKING_MIN_NUM_PREDICT
THINKING_ONLY = {"message": {"thinking": "long deliberation", "content": ""}, "done_reason": "length"}


@pytest.fixture(autouse=True)
def _fresh_thinking_state():
    saved_capability = dict(server._THINKING_CAPABILITY_CACHE)
    saved_unsupported = set(server._THINK_OPTION_UNSUPPORTED_CACHE)
    server._THINKING_CAPABILITY_CACHE.clear()
    server._THINK_OPTION_UNSUPPORTED_CACHE.clear()
    yield
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


def _ask(num_predict=260, **kwargs):
    return server._chat_request(
        {"model": "r", "messages": [{"role": "user", "content": "q"}],
         "options": {"num_predict": num_predict}},
        model="r", **kwargs,
    )


def _answers_only_without_thinking(payload, _index):
    if payload.get("think") is False:
        return {"message": {"content": "the real answer"}}
    return dict(THINKING_ONLY)


def test_a_model_that_thinks_through_the_headroom_is_asked_once_without_thinking(monkeypatch):
    seen = _record(monkeypatch, _answers_only_without_thinking)

    _out, content = _ask(260)

    assert content == "the real answer"
    # tight budget, then the headroom, then thinking off with the caller's own budget
    assert [budget for budget, _think in seen] == [260, MINIMUM, 260]
    assert seen[-1][1] is False
    assert all(think is not False for _budget, think in seen[:-1])


def test_a_known_thinking_model_is_not_sent_the_same_request_twice(monkeypatch):
    server._remember_thinking_model("r")
    seen = _record(monkeypatch, _answers_only_without_thinking)

    _out, content = _ask(260)

    assert content == "the real answer"
    # headroom was applied up front; the identical request is not repeated
    assert [budget for budget, _think in seen] == [MINIMUM, 260]
    assert seen[-1][1] is False


def test_a_generous_caller_budget_is_kept_for_the_ask_without_thinking(monkeypatch):
    seen = _record(monkeypatch, _answers_only_without_thinking)

    _out, content = _ask(6000)

    assert content == "the real answer"
    assert seen == [(6000, None), (6000, False)]


def test_the_original_failure_is_reported_when_the_second_ask_is_empty_too(monkeypatch):
    seen = _record(monkeypatch, lambda payload, _index: (
        {"message": {"content": ""}, "done_reason": "stop"} if payload.get("think") is False
        else dict(THINKING_ONLY)))

    with pytest.raises(server.ModelCallError) as caught:
        _ask(260)

    assert caught.value.kind == "empty_response"
    assert '"done_reason": "length"' in str(caught.value.detail)
    assert len(seen) == 3  # never a fourth request


def test_the_original_failure_is_reported_when_the_second_ask_fails(monkeypatch):
    def reply(payload, _index):
        if payload.get("think") is False:
            raise server.ModelCallError("http", "bad request", status=400)
        return dict(THINKING_ONLY)

    seen = _record(monkeypatch, reply)

    with pytest.raises(server.ModelCallError) as caught:
        _ask(260)

    assert caught.value.kind == "empty_response"
    assert len(seen) == 3


def test_a_cancelled_second_ask_stays_a_cancellation(monkeypatch):
    def reply(payload, _index):
        if payload.get("think") is False:
            raise server.ModelCallError("cancelled", "model call cancelled")
        return dict(THINKING_ONLY)

    _record(monkeypatch, reply)

    with pytest.raises(server.ModelCallError) as caught:
        _ask(260)

    assert caught.value.kind == "cancelled"


def test_a_model_that_refused_the_think_switch_is_not_asked_again(monkeypatch):
    server._remember_unsupported_think_option("r")
    seen = _record(monkeypatch, lambda _payload, _index: dict(THINKING_ONLY))

    with pytest.raises(server.ModelCallError) as caught:
        _ask(260)

    assert caught.value.kind == "empty_response"
    assert [budget for budget, _think in seen] == [260, MINIMUM]


def test_an_explicit_thinking_off_call_is_never_repeated(monkeypatch):
    seen = _record(monkeypatch, lambda _payload, _index: {"message": {"content": ""}, "done_reason": "length"})

    with pytest.raises(server.ModelCallError):
        server._chat_request(
            {"model": "r", "messages": [], "think": False, "options": {"num_predict": 260}}, model="r",
        )

    assert seen == [(260, False)]


def test_a_hosted_model_keeps_its_own_rule(monkeypatch):
    seen = _record(monkeypatch, lambda _payload, _index: dict(THINKING_ONLY))

    with pytest.raises(server.ModelCallError):
        server._chat_request(
            {"model": "custom:cloud", "messages": [], "think": True, "options": {"num_predict": 4096}},
            model="custom:cloud", cloud=True, idempotent=True,
        )

    assert seen == [(4096, True)]


def test_the_second_ask_is_recorded_as_an_activity_event(monkeypatch):
    events = []
    monkeypatch.setattr(server.activity_tracker, "record_event",
                        lambda name, **fields: events.append((name, fields)))
    _record(monkeypatch, _answers_only_without_thinking)

    _ask(260)

    assert [name for name, _fields in events].count("model_thinking_fallback") == 1
    assert dict(events)["model_thinking_fallback"]["model"] == "r"
