"""Pure model-tag sizing policy for local inference planning."""
from __future__ import annotations

import re


# Ollama/Qwen spell an MoE tag as ``<total>b-a<active>b``.  Dense tags carry
# one ``<n>b`` value.  Quantization and instruction suffixes are deliberately
# ignored after the size-bearing portion.
_MOE_TAG_RE = re.compile(
    r"(?<![0-9a-z])([0-9]+(?:\.[0-9]+)?)b-a"
    r"([0-9]+(?:\.[0-9]+)?)b(?![0-9a-z])"
)
_DENSE_TAG_RE = re.compile(
    r"(?<![0-9a-z])([0-9]+(?:\.[0-9]+)?)b(?![0-9a-z])"
)


def params_from_model_tag(tag) -> tuple[float, float] | None:
    """Return ``(total_params_b, active_params_b)`` from an Ollama tag.

    An alias without a size-bearing tag returns ``None``.  For a dense model
    both values are equal; for a mixture-of-experts model they differ.  The
    parser searches only the tag portion after ``:``, preventing a repository
    name such as ``custom-70b-model:latest`` from masquerading as a size.
    """
    text = str(tag or "").strip().lower()
    _name, separator, tag_part = text.rpartition(":")
    if not separator:
        return None
    text = tag_part.strip()
    if not text:
        return None

    moe = _MOE_TAG_RE.search(text)
    if moe:
        total = float(moe.group(1))
        active = float(moe.group(2))
        if total > 0 and 0 < active <= total:
            return total, active
        return None

    dense = _DENSE_TAG_RE.search(text)
    if dense:
        total = float(dense.group(1))
        if total > 0:
            return total, total
    return None
