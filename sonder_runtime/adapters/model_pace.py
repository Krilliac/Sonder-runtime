"""The measured writing speed of each local model in this process.

``observe`` records tokens a second from a model response.  ``up_front`` uses
the latest figure for a model known to think: one too slow to write its
thinking headroom inside the request timeout is asked without thinking, and
``fit`` cuts the headroom to what the timeout can deliver where thinking stays
on (the rules and their reason are in ``sonder_runtime.domain.thinking_pace``).
With no measurement yet a request is left exactly as it was, so the first call
to a model is never changed.
"""
from __future__ import annotations

import logging
import threading

from sonder_runtime.domain.thinking_pace import (
    affordable_tokens,
    fit_headroom,
    output_rate,
    without_thinking,
)

_logger = logging.getLogger("sonder.model_pace")
_LOCK = threading.Lock()
_RATES: dict[str, float] = {}


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
        _RATES[name] = measured


def rate(model: object) -> float | None:
    """The latest measured tokens a second for ``model``, or None.  Never does I/O."""
    with _LOCK:
        return _RATES.get(_key(model))


def fit(payload: dict, caller_payload: object, model: object, timeout_seconds: object) -> dict:
    """``payload`` with thinking headroom ``model`` can produce inside ``timeout_seconds``.

    Unchanged when the model has not been measured, and never below the reply
    budget in ``caller_payload``.
    """
    fitted = fit_headroom(payload, caller_payload, affordable_tokens(rate(model), timeout_seconds))
    if fitted is not payload:
        _logger.info(
            "thinking headroom for %s cut from %s to %s tokens: at %.1f tokens/s more would not arrive in %ss",
            _key(model)[:80], payload["options"].get("num_predict"), fitted["options"].get("num_predict"),
            rate(model) or 0.0, timeout_seconds,
        )
    return fitted


def up_front(payload: dict, caller_payload: object, model: object, timeout_seconds: object,
             can_skip_thinking: bool) -> dict:
    """The request to send to a model known to think; ``payload`` carries its thinking headroom.

    A measured model too slow to write that headroom inside ``timeout_seconds``
    is asked ``caller_payload`` with thinking off, when ``can_skip_thinking``
    (the model has not refused the switch) and neither request sets ``think``.
    Otherwise the headroom is fitted as in ``fit``.
    """
    plain = None
    if can_skip_thinking:
        plain = without_thinking(payload, caller_payload, affordable_tokens(rate(model), timeout_seconds))
    if plain is None:
        return fit(payload, caller_payload, model, timeout_seconds)
    _logger.info(
        "%s asked without thinking for %s tokens: at %.1f tokens/s its %s-token thinking budget would not arrive in %ss",
        _key(model)[:80], plain["options"].get("num_predict"), rate(model) or 0.0,
        payload["options"].get("num_predict"), timeout_seconds,
    )
    return plain


def reset() -> None:
    """Forget every measurement (tests)."""
    with _LOCK:
        _RATES.clear()
