"""Log-safe rendering of endpoint URLs for the inference adapters."""
from __future__ import annotations

from urllib.parse import urlsplit

_INVALID_URL = "<invalid url>"


def redact_url(value: object) -> str:
    """Render ``scheme://host[:port]/path`` with no userinfo, query or fragment.

    If every ``@`` precedes the first ``/``, ``?`` or ``#`` it is userinfo and
    everything up to the last one is dropped. An ``@`` after that delimiter is
    ambiguous (password with an unescaped delimiter, or path/query text), so
    the fixed placeholder ``<scheme>://<redacted>`` is rendered instead.

    Pure and thread-safe; never raises. Anything that cannot be parsed to a
    scheme and host renders as a fixed placeholder, never as the raw input.
    """
    try:
        text = str(value).strip()
        scheme, sep, rest = text.partition("://")
        if not sep or not scheme:
            return _INVALID_URL
        if "@" in rest:
            cut = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=-1)
            if cut >= 0 and rest.rfind("@") > cut:
                # An "@" after the first delimiter could be userinfo with an
                # unescaped delimiter or plain path/query text: not decidable.
                return "%s://<redacted>" % scheme.lower()
            rest = rest.rpartition("@")[2]
        parts = urlsplit("%s://%s" % (scheme, rest))
        if not parts.scheme or not parts.netloc:
            return _INVALID_URL
        return "%s://%s%s" % (parts.scheme.lower(), parts.netloc, parts.path)
    except Exception:
        return _INVALID_URL


__all__ = ["redact_url"]
