"""Range check for a caller-supplied per-request sampling temperature.

The inference servers reject (or crash on) a temperature outside their range,
so a bad value must be refused before any model call; otherwise the failure
looks like a model failure and can trigger a tier escalation that cannot help.
The env-var temperature has its own clamp (``serve_temperature``); this check
is only for values a caller passes on a request.
"""
from __future__ import annotations

import math
from typing import TypeVar

from .common.errors import InvalidInput

TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0
_SHOWN_MAX = 80  # an oversized value is not echoed back in full

_T = TypeVar("_T")


def validate_request_temperature(value: _T) -> _T:
    """Return ``value`` unchanged if it is ``None`` (unset) or a finite real
    number in [0.0, 2.0]; otherwise raise ``InvalidInput``.

    Booleans are refused: ``True`` would otherwise pass as 1.0.
    """
    if value is None:
        return value
    # An int is compared directly: math.isfinite(10**400) would overflow.
    finite = not isinstance(value, float) or math.isfinite(value)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not finite or not TEMPERATURE_MIN <= value <= TEMPERATURE_MAX):
        try:
            shown = repr(value)
        except ValueError:  # an int past the str-conversion digit limit
            shown = "<%s too large to show>" % type(value).__name__
        if len(shown) > _SHOWN_MAX:
            shown = shown[:_SHOWN_MAX - 3] + "..."
        raise InvalidInput(
            "temperature must be a finite number from 0 to 2, got %s" % shown
        )
    return value
