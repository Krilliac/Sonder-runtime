"""What a local model can write before its request times out.

A local reasoning model is given headroom (``LOCAL_THINKING_MIN_NUM_PREDICT``
tokens) so that its thinking does not use up the reply budget the caller
chose.  Headroom a model cannot produce inside the request timeout is not
headroom: the request times out with nothing, where a smaller budget would
have come back.  Seen live on 2026-11-13: a model writing 4.5 tokens a second
was given 4096 tokens under a 300 second timeout (about 1,300 tokens) and the
call failed with a read timeout; asked for 900 it answered in 188 seconds.

Cutting the headroom alone was not enough.  Measured the same day at 3.3
tokens a second: the request, cut back to the caller's 900 tokens, came back
after 284 seconds as 900 tokens of thinking and no answer, and the second ask
with thinking off took another 277 seconds.  A model that cannot write the
caller's reply plus ``MIN_THINKING_TOKENS`` of thought inside the timeout has
no time to think and then answer, so it is asked once, with thinking off, for
the reply the timeout can deliver (``without_thinking``).  Every other request
keeps thinking: its headroom is cut to what the model's measured speed can
deliver, never below the budget the caller asked for (``fit_headroom``).

Where the line falls, with the two models measured that day.  At 52.9 tokens a
second under the 45 second timeout of a fan-out call, 1,904 tokens are
affordable: a 512-token reply needs 1,024, so the model keeps thinking with
1,904 tokens of headroom.  At 3.4 tokens a second under 300 seconds, 811 are
affordable: a 900-token reply needs 1,412, so thinking is switched off.  A
request the caller sized at or above the headroom is given none and is never
changed here.  Pure functions over Ollama's own timing fields; nothing here
does I/O or keeps state.
"""
from __future__ import annotations

from collections.abc import Mapping

# A reply shorter than this says too little about the model's speed.
MIN_SAMPLE_TOKENS = 16
# Share of the timeout counted as writing time; the rest covers loading the
# model and reading the prompt (measured: 4 to 22 seconds of a 300 second call).
PACE_MARGIN = 0.8
# A reply budget is never cut below this, however slow the model measured.
MIN_FITTED_TOKENS = 64
# Thought a model must have room for, on top of the caller's reply, to be left
# thinking.  A chosen value: the measured thinking of one answer ran from about
# 120 tokens (a one-line question) to over 3,000 (a long design question).
MIN_THINKING_TOKENS = 512
# Weight of a new speed measurement against the figure already held, so that
# one reply written while the machine was busy does not decide the next request
# alone (the slower model measured 3.3 to 4.5 tokens a second within one day).
RATE_SMOOTHING = 0.5
_NANOSECONDS = 1_000_000_000


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def output_rate(response: object) -> float | None:
    """Tokens a second from one response's ``eval_count`` and ``eval_duration``, or None.

    None when the response does not carry both fields or the reply was too
    short to measure.  ``eval_duration`` is Ollama's generation time alone, in
    nanoseconds, so a cold model load does not make a fast model look slow.
    """
    if not isinstance(response, Mapping):
        return None
    tokens = _positive_int(response.get("eval_count"))
    duration = _positive_int(response.get("eval_duration"))
    if tokens is None or duration is None or tokens < MIN_SAMPLE_TOKENS:
        return None
    return tokens * _NANOSECONDS / duration


def smoothed_rate(previous: float | None, measured: float, weight: float = RATE_SMOOTHING) -> float:
    """The figure to hold after a new measurement: the first one as it is, later ones averaged in."""
    if previous is None:
        return measured
    return previous + (measured - previous) * weight


def affordable_tokens(rate: object, timeout_seconds: object, margin: float = PACE_MARGIN) -> int | None:
    """Tokens a model writing ``rate`` a second can produce inside ``timeout_seconds``, or None.

    None when either number is missing or not positive: with no measurement
    there is nothing to fit the budget to.
    """
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        return None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return None
    return int(rate * timeout_seconds * margin)


def _num_predict(payload: object) -> int | None:
    options = payload.get("options") if isinstance(payload, Mapping) else None
    return _positive_int(options.get("num_predict")) if isinstance(options, Mapping) else None


def fit_headroom(payload: dict, caller_payload: object, affordable: int | None) -> dict:
    """``payload`` with its thinking headroom cut to ``affordable`` tokens.

    Only headroom is cut: the result never asks for less than the caller's own
    ``num_predict`` (``caller_payload``), and a payload that carries no headroom
    over it comes back unchanged.  Returns ``payload`` itself when nothing
    changes, a copy otherwise; the caller's dict is never mutated.
    """
    budget, asked = _num_predict(payload), _num_predict(caller_payload)
    if affordable is None or budget is None or asked is None or budget <= asked:
        return payload
    fitted = max(asked, min(budget, affordable))
    if fitted == budget:
        return payload
    fitted_payload = dict(payload)
    fitted_payload["options"] = dict(payload["options"], num_predict=fitted)
    return fitted_payload


def retry_with_room(payload: dict, with_headroom: dict, caller_payload: object, affordable: int | None) -> dict:
    """The request to send again after a reply that was all thought, or ``payload`` itself.

    ``payload`` was answered with thinking and no content; ``with_headroom`` is
    the same request with the thinking headroom added.  The retry is worth a
    request only when it gives the model room it has not had: a ``payload``
    that already carried headroom over the caller's budget comes back as it
    is, and so does one for which no larger budget is affordable.  Seen live
    on 2026-11-13: a request fitted to 1,897 tokens came back all thought, the
    speed measured from it moved the fitted figure to 1,887, and the request
    was sent a second time with ten tokens less.
    """
    current, asked = _num_predict(payload), _num_predict(caller_payload)
    if current is not None and asked is not None and current > asked:
        return payload
    fitted = fit_headroom(with_headroom, caller_payload, affordable)
    return fitted if (_num_predict(fitted) or 0) > (current or 0) else payload


def without_thinking(payload: dict, caller_payload: object, affordable: int | None) -> dict | None:
    """The request to send with thinking off, or None when the model has time to think.

    ``payload`` is the request with the thinking headroom added.  A model that
    cannot write the caller's reply and ``MIN_THINKING_TOKENS`` of thought
    inside the timeout (``affordable`` is under their sum) is asked the
    caller's own request with ``think`` False, its reply budget cut to
    ``affordable`` when even that would not arrive (never under
    ``MIN_FITTED_TOKENS``).

    None, so the caller keeps thinking on, when nothing was measured, when the
    reply and that much thought are affordable, when the request was given no
    headroom over the caller's own budget, when either request already says
    what to do about thinking, or when a budget cannot be read.  A new dict;
    the inputs are never mutated.
    """
    budget, asked = _num_predict(payload), _num_predict(caller_payload)
    if affordable is None or budget is None or asked is None or budget <= asked:
        return None
    if affordable >= asked + MIN_THINKING_TOKENS:
        return None
    if "think" in payload or "think" in caller_payload:
        return None
    plain = dict(caller_payload)
    plain["think"] = False
    plain["options"] = dict(caller_payload["options"],
                            num_predict=min(asked, max(affordable, MIN_FITTED_TOKENS)))
    return plain
