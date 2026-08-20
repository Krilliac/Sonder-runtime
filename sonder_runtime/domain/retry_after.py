"""Pure policy for interpreting upstream HTTP ``Retry-After`` hints."""

import datetime
import email.utils
import math


def retry_after_seconds(headers, *, now=None):
    """Return a bounded upstream Retry-After hint without sleeping on it.

    Both RFC delta-seconds and HTTP-date forms are accepted. Malformed,
    non-finite, and excessive values are ignored or capped so an upstream
    cannot make runtime status output misleading.
    """
    try:
        value = headers.get("Retry-After", "") if headers else ""
    except (AttributeError, TypeError):
        value = ""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            current = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            seconds = (when - current).total_seconds()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    if seconds < 0:
        return 0.0
    return min(float(seconds), 86400.0)
