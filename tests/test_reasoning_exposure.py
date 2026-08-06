"""Reasoning exposure: off by default, gated by audience, never cross-turn."""
import activity_tracker as at
import server
import sonder_serve as ts

import pytest


@pytest.fixture(autouse=True)
def _clean_tracker():
    at.reset_for_tests()
    yield
    at.reset_for_tests()


def test_exposure_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SONDER_EXPOSE_REASONING", raising=False)
    assert server.reasoning_exposure_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_exposure_enables_on_truthy_flag(monkeypatch, value):
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", value)
    assert server.reasoning_exposure_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_exposure_stays_off_on_falsy_flag(monkeypatch, value):
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", value)
    assert server.reasoning_exposure_enabled() is False


def test_audience_defaults_to_developer(monkeypatch):
    monkeypatch.delenv("SONDER_REASONING_AUDIENCE", raising=False)
    assert ts._reasoning_audience() == "developer"
    monkeypatch.setenv("SONDER_REASONING_AUDIENCE", "all")
    assert ts._reasoning_audience() == "all"
    # Anything unrecognised must fall back to the narrower audience.
    monkeypatch.setenv("SONDER_REASONING_AUDIENCE", "everyone")
    assert ts._reasoning_audience() == "developer"


def test_reasoning_never_visible_while_exposure_is_off(monkeypatch):
    monkeypatch.delenv("SONDER_EXPOSE_REASONING", raising=False)
    monkeypatch.setenv("SONDER_REASONING_AUDIENCE", "all")
    assert ts._reasoning_visible_to({"mode": "local-open"}) is False


def test_developer_audience_withholds_from_plain_callers(monkeypatch):
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    monkeypatch.setenv("SONDER_REASONING_AUDIENCE", "developer")
    plain = {"mode": "api-key", "authorized": True, "api_key": False, "account": None}
    assert ts._reasoning_visible_to(plain) is False
    assert ts._reasoning_visible_to({"mode": "local-open"}) is True


def test_all_audience_reaches_plain_callers(monkeypatch):
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    monkeypatch.setenv("SONDER_REASONING_AUDIENCE", "all")
    plain = {"mode": "api-key", "authorized": True, "api_key": False, "account": None}
    assert ts._reasoning_visible_to(plain) is True


def test_reasoning_is_kept_out_of_the_activity_snapshot():
    """sonder_activity goes to every client; reasoning is gated separately."""
    with at.response_span("t", "p"):
        at.record_reasoning("secret deliberation", model="m")
        snapshot = at.snapshot()
    assert "secret deliberation" not in repr(snapshot)
    assert "secret deliberation" not in repr(at.snapshot())


def test_current_reasoning_does_not_leak_the_previous_turn():
    """The HTTP path reads reasoning INSIDE its own still-open span.

    Reading _LATEST_REASONING there would attach turn one's reasoning to turn
    two's answer, so current_reasoning() must be scoped to the open span.
    """
    with at.response_span("turn-one", "p"):
        at.record_reasoning("first turn thought", model="m")
    assert at.latest_reasoning()["text"] == "first turn thought"

    with at.response_span("turn-two", "p"):
        # Nothing recorded yet for this turn: must be empty, not turn one's.
        assert at.current_reasoning() is None
        at.record_reasoning("second turn thought", model="m")
        assert at.current_reasoning()["text"] == "second turn thought"


def test_segments_accumulate_in_order():
    with at.response_span("t", "p"):
        at.record_reasoning("alpha", model="m")
        at.record_reasoning("beta", model="m")
        record = at.current_reasoning()
    assert record["segments"] == 2
    assert record["text"].index("alpha") < record["text"].index("beta")


def test_blank_reasoning_is_ignored():
    with at.response_span("t", "p"):
        at.record_reasoning("   ", model="m")
        at.record_reasoning(None, model="m")
        assert at.current_reasoning() is None


def test_reasoning_is_truncated_to_a_bound():
    with at.response_span("t", "p"):
        at.record_reasoning("x" * (at.MAX_REASONING_CHARS + 500), model="m")
        record = at.current_reasoning()
    assert record["truncated"] is True
    assert len(record["text"]) < at.MAX_REASONING_CHARS + 200


def test_completion_object_omits_reasoning_when_empty():
    assert "sonder_reasoning" not in ts._chat_completion_object("hi", "sonder")
    obj = ts._chat_completion_object("hi", "sonder", reasoning="because")
    assert obj["sonder_reasoning"] == "because"


def test_turn_reasoning_is_empty_while_exposure_is_off(monkeypatch):
    monkeypatch.delenv("SONDER_EXPOSE_REASONING", raising=False)
    with at.response_span("t", "p"):
        at.record_reasoning("thought", model="m")
        assert ts._turn_reasoning() == ""
