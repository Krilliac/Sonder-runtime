"""Pure parsing policy for interaction identifiers appended to responses."""

from __future__ import annotations


DEFAULT_FOOTER_PREFIX = "\n\n[interaction_id: "


def trailing_interaction_id(
    text: object,
    footer_prefix: str = DEFAULT_FOOTER_PREFIX,
) -> str | None:
    """Return the opaque interaction id from a well-formed response footer.

    The footer's own delimiter is authoritative: the identifier is intentionally
    not restricted to the current store's lowercase-hex format.
    """
    body = (text or "").rstrip()
    start = body.rfind(footer_prefix)
    if start < 0 or not body.endswith("]"):
        return None
    return body[start + len(footer_prefix):-1].strip() or None


__all__ = ["DEFAULT_FOOTER_PREFIX", "trailing_interaction_id"]
