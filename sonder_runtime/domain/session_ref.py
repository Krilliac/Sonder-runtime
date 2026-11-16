"""Normalise a shared chat reference to a bare session id.

A chat can be referred to three ways, all local to this machine:

* the bare id: ``chat-1794840126388000``
* the chat code: ``sonder:chat/chat-1794840126388000``
* the web link: ``http://127.0.0.1:8000/app/#/c/chat-1794840126388000``

:func:`parse_session_ref` turns any of them into the id, or ``None`` when the
text is not a well-formed reference.  Ids are validated strictly (no path
characters, bounded length) so a normalised id is safe to put in a URL path or
a store lookup.  Pure stdlib; no I/O.
"""
from __future__ import annotations

import re

SESSION_ID_MAX_LENGTH = 128
# Longest accepted reference text: a link carries an origin on top of the id.
SESSION_REF_MAX_LENGTH = 2048
CHAT_CODE_PREFIX = "sonder:chat/"

_ID = r"[A-Za-z0-9_.\-]{1,%d}" % SESSION_ID_MAX_LENGTH
_ID_RE = re.compile(r"\A%s\Z" % _ID)
_CODE_RE = re.compile(r"\Asonder:chat/(%s)\Z" % _ID)
# Optional ``scheme://host[:port]``, optional ``/app``, then the hash route.
_LINK_RE = re.compile(r"\A(?:https?://[^\s/#?]+)?(?:/app/?)?#/c/(%s)/?\Z" % _ID)


def is_valid_session_id(value: object) -> bool:
    """True when ``value`` is a well-formed session id (``[A-Za-z0-9_.-]{1,128}``)."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        return False
    # ``.`` and ``..`` match the character class but are path components.
    return value.strip(".") != ""


def parse_session_ref(text: object) -> str | None:
    """Return the session id named by an id, chat code or ``/app/#/c/<id>`` link."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate or len(candidate) > SESSION_REF_MAX_LENGTH:
        return None
    for pattern in (_CODE_RE, _LINK_RE):
        match = pattern.match(candidate)
        if match:
            candidate = match.group(1)
            break
    return candidate if is_valid_session_id(candidate) else None


def chat_code(session_id: str) -> str:
    """The shareable code for ``session_id``; raises ValueError for a bad id."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session id")
    return CHAT_CODE_PREFIX + session_id


__all__ = ["CHAT_CODE_PREFIX", "SESSION_ID_MAX_LENGTH", "SESSION_REF_MAX_LENGTH",
           "chat_code", "is_valid_session_id", "parse_session_ref"]
