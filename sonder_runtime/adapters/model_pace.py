"""The measured writing speed of each local model in this process.

``observe`` keeps the last few tokens-a-second measurements of each model.
``up_front`` uses them for a model known to think: one that even at its
fastest recent speed is too slow to write the caller's reply and a minimum of
thought inside the request timeout is asked without thinking, or, where it has
to keep thinking, with its headroom cut (``fit``); what is asked for is sized
on its slowest recent speed.  A model with time for both is sent its usual
request (the rules and their reason are in
``sonder_runtime.domain.thinking_pace``).

The speed is held per model name.  The worker pool can serve one model from
machines of different speed and does not say which one answered, which is why
the fastest recent measurement decides: a model is treated as slow only after
``RATE_WINDOW`` measurements in a row were slow.
With no measurement yet a request is left exactly as it was, so the first call
to a model is never changed.
"""
from __future__ import annotations

import logging
import threading
from collections import deque

from sonder_runtime.domain.thinking_pace import (
    MIN_THINKING_TOKENS,
    RATE_WINDOW,
    affordable_tokens,
    fit_headroom,
    output_rate,
    retry_with_room,
    without_thinking,
)

_logger = logging.getLogger("sonder.model_pace")
_LOCK = threading.Lock()
_RATES: dict[str, deque[float]] = {}


def _key(model: object) -> str:
    return str(model or "").strip()


def observe(model: object, response: object, *, local: bool = True) -> None:
    """Remember how fast ``model`` wrote ``response``.  Hosted calls are not measured."""
    if not local:
        return
    name = _key(model)
    measured = output_rate(response)
    if not name or measured is None:
        return
    with _LOCK:
        _RATES.setdefault(name, deque(maxlen=RATE_WINDOW)).append(measured)


def rate(model: object) -> float | None:
    """The fastest recent tokens a second measured for ``model``, or None.  Never does I/O."""
    with _LOCK:
        recent = _RATES.get(_key(model))
        return max(recent) if recent else None


def slowest(model: object) -> float | None:
    """The slowest recent tokens a second measured for ``model``, or None.  Never does I/O."""
    with _LOCK:
        recent = _RATES.get(_key(model))
        return min(recent) if recent else None


def _affordable(model: object, timeout_seconds: object) -> tuple[int | None, int | None]:
    """Tokens ``model`` can write inside ``timeout_seconds`` at its fastest and at its slowest recent speed."""
    with _LOCK:
        recent = tuple(_RATES.get(_key(model)) or ())
    if not recent:
        return None, None
    return affordable_tokens(max(recent), timeout_seconds), affordable_tokens(min(recent), timeout_seconds)


def fit(payload: dict, caller_payload: object, model: object, timeout_seconds: object) -> dict:
    """``payload`` with thinking headroom ``model`` can produce inside ``timeout_seconds``.

    Unchanged when the model has not been measured or has time for the reply
    and a minimum of thought, and never below the reply budget in
    ``caller_payload``.
    """
    fitted = fit_headroom(payload, caller_payload, *_affordable(model, timeout_seconds))
    if fitted is not payload:
        _logger.info(
            "thinking headroom for %s cut from %s to %s tokens: at %.1f tokens/s more would not arrive in %ss",
            _key(model)[:80], payload["options"].get("num_predict"), fitted["options"].get("num_predict"),
            slowest(model) or 0.0, timeout_seconds,
        )
    return fitted


def room_to_retry(payload: dict, with_headroom: dict, caller_payload: object, model: object,
                  timeout_seconds: object) -> dict:
    """The retry for a reply that was all thought, or ``payload`` itself when there is no more room to give."""
    return retry_with_room(payload, with_headroom, caller_payload, *_affordable(model, timeout_seconds))


def up_front(payload: dict, caller_payload: object, model: object, timeout_seconds: object,
             can_skip_thinking: bool) -> dict:
    """The request to send to a model known to think; ``payload`` carries its thinking headroom.

    A measured model too slow to write the caller's reply and a minimum of
    thought inside ``timeout_seconds`` is asked ``caller_payload`` with thinking
    off, when ``can_skip_thinking`` (the model has not refused the switch) and
    neither request sets ``think``.  Otherwise the headroom is fitted as in
    ``fit``.

    A model that refuses the switch on that request is asked again by the
    transport without it and thinks inside the reply budget; if it writes no
    answer that call fails as an empty response.  The refusal is remembered, so
    later calls keep thinking with the fitted headroom.
    """
    plain = None
    if can_skip_thinking:
        plain = without_thinking(payload, caller_payload, *_affordable(model, timeout_seconds))
    if plain is None:
        return fit(payload, caller_payload, model, timeout_seconds)
    _logger.info(
        "%s asked without thinking for %s tokens (reply cut if under the %s asked): at %.1f tokens/s a %s-token reply and %s tokens of thought would not arrive in %ss",
        _key(model)[:80], plain["options"].get("num_predict"), caller_payload["options"].get("num_predict"), rate(model) or 0.0,
        caller_payload["options"].get("num_predict"), MIN_THINKING_TOKENS, timeout_seconds,
    )
    return plain


def reset() -> None:
    """Forget every measurement (tests)."""
    with _LOCK:
        _RATES.clear()
