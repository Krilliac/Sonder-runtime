"""Pure policy for removing private inline model reasoning from output."""

from __future__ import annotations

import re


_INLINE_THINKING_OPEN_RE = re.compile(
    r"^\s*<(think|thinking)(?:\s+[^>]*)?>", re.IGNORECASE,
)
_INLINE_THINKING_TAG_RE = re.compile(
    r"</?(think|thinking)(?:\s+[^>]*)?>", re.IGNORECASE,
)


def thinking_exhausted_budget(out, message, *, inline_thinking=False) -> bool:
    """Return whether reasoning consumed an output budget before an answer."""
    if not isinstance(message, dict):
        return False
    thinking = message.get("thinking")
    if not inline_thinking and (not isinstance(thinking, str) or not thinking.strip()):
        return False
    done_reason = out.get("done_reason") if isinstance(out, dict) else None
    return str(done_reason or "").strip().casefold() == "length"


def strip_inline_thinking(content):
    """Drop closed leading model reasoning tags from public assistant text.

    Only leading, syntactically closed blocks are recognized. This keeps a
    legitimate answer that discusses literal tags intact while ensuring that
    untrusted model deliberation cannot be shown, saved to session history, or
    fed into a later turn as assistant content.
    """
    if not isinstance(content, str):
        return content
    value = content
    while True:
        opening = _INLINE_THINKING_OPEN_RE.match(value)
        if not opening:
            return value
        depth = 0
        end = None
        for tag in _INLINE_THINKING_TAG_RE.finditer(value, opening.start()):
            if tag.group(0).startswith("</"):
                depth -= 1
                if depth == 0:
                    end = tag.end()
                    break
            else:
                depth += 1
        # A leading unterminated reasoning block is private by default; never
        # trade an incomplete delimiter for a reasoning exposure.
        if end is None:
            return ""
        value = value[end:].lstrip()
