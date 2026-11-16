"""Log-safe rendering of endpoint URLs for the inference adapters.

Nothing here echoes its input. Every character that is rendered was matched
by one of the allowlist patterns below, and every other input becomes a fixed
token, so userinfo, query strings, fragments, ``;params`` and the raw text of
a malformed URL cannot reach a log line or an exception message through this
module, whatever the input looks like. ``urlsplit_or_none`` keeps urllib's own
error text, which quotes the URL, out of exception chains.
"""
from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit

_INVALID_URL = "<invalid url>"
_REDACTED_PATH = "/<redacted>"
_MAX_LENGTH = 2048
_SCHEMES = frozenset({"http", "https"})
# RFC 3986 scheme, accepted only as a prefix of the whole value.
_SCHEME_PREFIX = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://")
# A DNS name or IPv4 literal, or a bracketed IPv6 literal (no zone id), with an
# optional decimal port.
_HOST_PORT = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._\-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\])(?::([0-9]{1,5}))?"
)
_PATH = re.compile(r"(?:/[A-Za-z0-9._~%:+,=\-]*)*")


def _allowlisted(value: object) -> tuple[str, str, str] | str:
    """``(scheme, host[:port], path)`` built from allowlisted text, or a fixed token.

    ``scheme`` is ``""`` for a bare ``host:port[/path]``.
    """
    if not isinstance(value, str):
        return _INVALID_URL
    text = value.strip()
    if not text or len(text) > _MAX_LENGTH:
        return _INVALID_URL
    prefix = _SCHEME_PREFIX.match(text)
    if prefix is None:
        # No scheme: only a bare ``host:port[/path]`` is rendered. With an "@"
        # the value is a credential or an address, never an endpoint to show.
        if "@" in text:
            return _INVALID_URL
        scheme, rest = "", text
    else:
        scheme = prefix.group(1).lower()
        if scheme not in _SCHEMES:
            return _INVALID_URL
        rest = text[prefix.end():]
    cut = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if 0 <= found < cut:
            cut = found
    authority, tail = rest[:cut], rest[cut:]
    if "@" in tail:
        # An "@" after the authority could be a password holding an unescaped
        # "/", "?" or "#", in which case the "host" before it is a user name.
        # Not decidable, so not even the host is rendered.
        return "%s://<redacted>" % scheme
    host_port = _HOST_PORT.fullmatch(authority.rpartition("@")[2])
    if host_port is None:
        return _INVALID_URL
    host, port = host_port.group(1).lower(), host_port.group(2)
    if port is None:
        if not scheme:
            return _INVALID_URL
    elif int(port) > 65535:
        return _INVALID_URL
    path = tail
    for delimiter in "?#":
        path = path.partition(delimiter)[0]
    # RFC 3986 allows ";params" on every segment, not only the last one.
    path = "/".join(segment.partition(";")[0] for segment in path.split("/"))
    if _PATH.fullmatch(path) is None:
        path = _REDACTED_PATH
    return scheme, host if port is None else "%s:%s" % (host, port), path


def redact_url(value: object) -> str:
    """Render ``[scheme://]host[:port][/path]`` for a log line or an error message.

    Rendered: an ``http``/``https`` URL, or a bare ``host:port[/path]``. The
    scheme and host are lower-cased; userinfo, the query, the fragment and
    every ``;params`` are dropped; a path with any other character than
    ``A-Z a-z 0-9 . _ ~ % : + , = - /`` becomes ``/<redacted>``.

    Not rendered, as a fixed token that holds none of the input:
    ``<scheme>://<redacted>`` when an ``@`` follows the authority, and
    ``<invalid url>`` for everything else (another scheme, no host, a bad
    port, a non-string, more than 2048 characters).

    Pure and thread-safe; never raises.
    """
    try:
        parsed = _allowlisted(value)
        if isinstance(parsed, str):
            return parsed
        scheme, host_port, path = parsed
        return "%s://%s%s" % (scheme, host_port, path) if scheme else host_port + path
    except Exception:
        return _INVALID_URL


def safe_origin(value: object) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, or None when it is not renderable.

    The same allowlist as ``redact_url``: None for a value with no scheme, with
    an ``@`` after its authority, or with a malformed host or port. Never raises.
    """
    try:
        parsed = _allowlisted(value)
        if isinstance(parsed, str) or not parsed[0]:
            return None
        return "%s://%s" % (parsed[0], parsed[1])
    except Exception:
        return None


def urlsplit_or_none(value: str) -> SplitResult | None:
    """``urlsplit(value)`` with its port already validated, or None if urllib rejects it.

    urllib's ValueError quotes the netloc or port it rejected, and that text
    can be a password. Call this instead of catching the error, then raise a
    fixed-text error of your own: raised outside any ``except`` block, it has
    neither ``__cause__`` nor ``__context__`` to carry urllib's message.
    """
    try:
        parts = urlsplit(value)
        _ = parts.port  # urllib validates the port only when it is read
    except Exception:
        return None
    return parts if isinstance(parts, SplitResult) else None


__all__ = ["redact_url", "safe_origin", "urlsplit_or_none"]
