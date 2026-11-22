"""Which sampling temperature a chat turn uses, and where it came from.

Interactive chat no longer sends a blanket 0.2.  The order is:

1. ``request`` -- a number the caller passed on this request (MCP ``sonder``
   ``temperature``, a forced per-thread value such as Spanda's samples);
2. ``session`` -- the value set with ``/temp`` (or the GUI slider) on the
   conversation thread;
3. ``lane`` -- a value the calling lane pins (``SONDER_SERVE_TEMPERATURE``
   on the serve route, the code-repair round trip);
4. ``preset`` -- nothing is sent, so the model family's preset applies
   (the Ollama Modelfile, or ``request_tuning``'s Qwen3.x table on
   Sonder Inference: 0.7 non-thinking, 1.0 thinking).

Deterministic lanes (router, reference lookup, conformance probes, computer
use, security evals) pass their own 0 directly and never come through here.

The range rule is owned by :mod:`sonder_runtime.domain.request_temperature`:
a finite real number from 0 to 2.
"""
from __future__ import annotations

from dataclasses import dataclass

from .common.errors import InvalidInput
from .request_temperature import (
    TEMPERATURE_MAX,
    TEMPERATURE_MIN,
    validate_request_temperature,
)

SOURCE_REQUEST = "request"
SOURCE_SESSION = "session"
SOURCE_LANE = "lane"
SOURCE_PRESET = "preset"

# Words that clear a session value back to the model preset.
AUTO_WORDS = frozenset({"auto", "default", "preset", "reset", "off", "none", "clear"})


def check_temperature(value: object) -> float | None:
    """Return ``value`` as a float, ``None`` for unset, or raise ``InvalidInput``.

    Delegates directly to :func:`validate_request_temperature` so there is a
    single source of truth for the [0, 2] finite-real range check.
    """
    checked = validate_request_temperature(value)
    if checked is None:
        return None
    assert isinstance(checked, (int, float))
    return float(checked)


def parse_temperature_setting(text: object) -> float | None:
    """Parse a ``/temp`` argument: a number in range, or ``auto`` (``None``)."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise InvalidInput("say a temperature from 0 to 2, or auto")
    if raw.lower() in AUTO_WORDS:
        return None
    try:
        number = float(raw)
    except ValueError:
        validate_request_temperature(raw)
        raise  # pragma: no cover - validate_request_temperature always raises on str
    return check_temperature(number)


@dataclass(frozen=True)
class ResolvedTemperature:
    """The temperature a turn sends (``None``: none sent) and its source."""

    value: float | None
    source: str

    def describe(self) -> str:
        shown = "auto" if self.value is None else format_temperature(self.value)
        return "effective temperature=%s (source: %s)" % (shown, self.source)


def format_temperature(value: float | None) -> str:
    if value is None:
        return "auto"
    text = ("%.2f" % value).rstrip("0").rstrip(".")
    return text or "0"


def resolve_temperature(
    *, request: object = None, session: object = None, lane: object = None,
) -> ResolvedTemperature:
    """Pick the first set value in the order request > session > lane > preset.

    Each given value is range-checked; a bad one raises ``InvalidInput``
    rather than being skipped, so a typo never silently falls through.
    """
    for source, value in (
        (SOURCE_REQUEST, request), (SOURCE_SESSION, session), (SOURCE_LANE, lane),
    ):
        checked = check_temperature(value)
        if checked is not None:
            return ResolvedTemperature(checked, source)
    return ResolvedTemperature(None, SOURCE_PRESET)


__all__ = [
    "AUTO_WORDS",
    "ResolvedTemperature",
    "SOURCE_LANE",
    "SOURCE_PRESET",
    "SOURCE_REQUEST",
    "SOURCE_SESSION",
    "TEMPERATURE_MAX",
    "TEMPERATURE_MIN",
    "check_temperature",
    "format_temperature",
    "parse_temperature_setting",
    "resolve_temperature",
]
