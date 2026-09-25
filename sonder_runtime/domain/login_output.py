"""Separate a login's bearer token from what a console may display.

``admin_login`` returns ``login ok``, the account line, and ``token: <t>``:
that is its MCP/API contract, and a protocol client needs the token. A
console that parses the token into its own session must not also print it --
terminal scrollback, ``repl --json`` stdout captured by a wrapper, and screen
shares all outlive the session. Consoles call :func:`split_login_output` and
print only the display form.
"""
from __future__ import annotations

TOKEN_MARKER = "token: "
HIDDEN_TOKEN_LINE = "token: [hidden] (held for this session only; never printed)"
_ERROR_PREFIX = "ERROR:"


def split_login_output(output: object) -> tuple[str, str]:
    """Return ``(token, display)`` for one ``admin_login`` result.

    ``token`` is "" for an error or a result without a token line, in which
    case ``display`` is the result unchanged. Otherwise every ``token:`` line
    is replaced by :data:`HIDDEN_TOKEN_LINE` and any other occurrence of the
    token value is removed from ``display``.
    """
    text = str(output or "")
    if text.startswith(_ERROR_PREFIX) or TOKEN_MARKER not in text:
        return "", text
    remainder = text.split(TOKEN_MARKER, 1)[1].strip()
    token = remainder.splitlines()[0].strip() if remainder else ""
    if not token:
        return "", text
    lines = [
        HIDDEN_TOKEN_LINE if line.strip().startswith(TOKEN_MARKER) else line
        for line in text.splitlines()
    ]
    display = "\n".join(lines).replace(token, "[hidden]")
    return token, display


__all__ = ["HIDDEN_TOKEN_LINE", "TOKEN_MARKER", "split_login_output"]
