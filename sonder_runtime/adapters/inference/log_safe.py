"""Log-safe rendering of endpoint URLs for the inference adapters."""
from __future__ import annotations

from urllib.parse import urlsplit

_INVALID_URL = "<invalid url>"


def redact_url(value: object) -> str:
    """Render ``scheme://host[:port]/path`` with no userinfo, query or fragment.

    An unescaped ``/``, ``?`` or ``#`` inside a password makes ``urlsplit``
    end the authority early, so any ``@`` after the scheme is treated as the
    end of userinfo and everything before the last one is dropped.

    Pure and thread-safe; never raises. Anything that cannot be parsed to a
    scheme and host renders as a fixed placeholder, never as the raw input.
    """
    try:
        text = str(value).strip()
        scheme, sep, rest = text.partition("://")
        if not sep or not scheme:
            return _INVALID_URL
        if "@" in rest:
            rest = rest.rpartition("@")[2]
        parts = urlsplit("%s://%s" % (scheme, rest))
        if not parts.scheme or not parts.netloc:
            return _INVALID_URL
        return "%s://%s%s" % (parts.scheme.lower(), parts.netloc, parts.path)
    except Exception:
        return _INVALID_URL


__all__ = ["redact_url"]
