"""Opaque destination labels safe to retain in public mobility receipts."""

import re


def is_public_mobility_label(value: object) -> bool:
    """Accept 1–128 ASCII label characters, excluding URL and path syntax."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) is not None
    )
