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

# Parameter-count bands describe the model itself, independently of the host
# capacity bands used by the hardware recommender.  Keeping this ladder here
# lets callers answer both "how fast will it decode?" and "how much memory do
# its weights need?" without importing the hardware probe module.
_PARAM_BANDS = (
    (5.0, "3-4B"),
    (13.0, "7B"),
    (40.0, "13-34B"),
    (float("inf"), "70B+"),
)
_BAND_ORDER = tuple(label for _ceiling, label in _PARAM_BANDS)
_Q4_GB_PER_BILLION = 0.62


def _band_for_parameter_count(value: float, ladder) -> str:
    for ceiling, label in ladder:
        if value < ceiling:
            return label
    return ladder[-1][1]


def decode_band(active_params_b) -> str | None:
    """Return the model band associated with its active parameter count."""
    try:
        active = float(active_params_b)
    except (TypeError, ValueError):
        return None
    if active <= 0:
        return None
    return _band_for_parameter_count(active, _PARAM_BANDS)


def memory_band(total_params_b) -> str | None:
    """Return the resident-memory class associated with total parameters."""
    try:
        total = float(total_params_b)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return _band_for_parameter_count(total, _PARAM_BANDS)


def band_fits(model_band: str, capacity_band: str) -> bool | None:
    """Whether a model band fits inside a host capacity band."""
    if model_band not in _BAND_ORDER or capacity_band not in _BAND_ORDER:
        return None
    return _BAND_ORDER.index(model_band) <= _BAND_ORDER.index(capacity_band)


def estimated_footprint_gb(total_params_b) -> float | None:
    """Estimate Q4-class resident weight size in GB for planning purposes."""
    try:
        total = float(total_params_b)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(total * _Q4_GB_PER_BILLION, 1)


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
