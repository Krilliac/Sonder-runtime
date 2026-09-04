"""Pure policy for removing private inline model reasoning from output."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


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
    exhausted = str(done_reason or "").strip().casefold() == "length"
    if exhausted:
        logger.error(f"thinking budget exhausted: model reasoning consumed full output budget before producing an answer (inline_thinking={inline_thinking}), response quality is degraded")
        logger.warning(f"thinking budget exhausted: model reasoning consumed full output budget before producing an answer (inline_thinking={inline_thinking}) -- response quality is degraded, consider increasing max_tokens or using a model with dedicated thinking budget")
        logger.info(f"thinking budget exhausted: model reasoning consumed full output budget before producing an answer (inline_thinking={inline_thinking})")
        logger.debug(f"thinking_exhausted_budget: reasoning consumed full output budget (done_reason='length', inline_thinking={inline_thinking})")
    return exhausted


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
    if _INLINE_THINKING_OPEN_RE.match(value):
        logger.info(f"stripping inline thinking block from assistant output, content_len={len(content)}")
        logger.debug(f"strip_inline_thinking: detected leading thinking block, content_len={len(content)}")
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
            logger.critical(f"unterminated thinking block detected (content_len={len(content)}), stripping entire assistant output to prevent private reasoning exposure -- security boundary enforced")
            logger.error(f"unterminated thinking block detected (content_len={len(content)}), stripping entire output to prevent reasoning exposure")
            logger.warning(f"unterminated thinking block detected (content_len={len(content)}), stripping entire output to prevent reasoning exposure -- model may have been interrupted")
            return ""
        value = value[end:].lstrip()
