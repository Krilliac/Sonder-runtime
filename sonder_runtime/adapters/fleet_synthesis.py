"""Small, provider-neutral helpers for bounded fleet synthesis.

The server owns provider selection and transport.  This module only supplies
the extra output headroom needed by bridged ``sonder-inference`` tiers and
adds an honest marker when a provider reports that generation ended at its
length limit.  It deliberately consumes the generator's existing sanitized
``last_response_meta`` attribute; provider response bodies are never parsed or
invented here.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping


SUMMARY_TRUNCATED_MARKER = "(summary truncated at the output limit)"
DEFAULT_SYNTHESIS_NUM_PREDICT = 2048
BRIDGED_REASONING_RESERVE = 2048
MAX_BRIDGED_SYNTHESIS_NUM_PREDICT = 16384


def json_num_predict(
    requested: int,
    *,
    bridged: bool = False,
    reasoning_reserve: int = BRIDGED_REASONING_RESERVE,
    maximum: int = MAX_BRIDGED_SYNTHESIS_NUM_PREDICT,
) -> int:
    """Return a bounded synthesis budget.

    Ollama and hosted callers retain their requested value.  A bridged
    ``sonder-inference`` call gets bounded extra room because its provider may
    spend output tokens on hidden reasoning before the visible JSON/text
    answer.  This is the same budget-preservation shape introduced for agent
    JSON generation in PR #616, kept here as a dependency-free adapter until
    that helper exists on this branch.  The caller must pass the returned value
    when constructing the generator; this function cannot safely change a
    generator that has already been created.
    """
    try:
        value = int(requested)
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_SYNTHESIS_NUM_PREDICT
    value = max(1, value)
    if not bridged:
        return value
    try:
        reserve = max(0, int(reasoning_reserve))
        cap = max(1, int(maximum))
    except (TypeError, ValueError, OverflowError):
        reserve, cap = BRIDGED_REASONING_RESERVE, MAX_BRIDGED_SYNTHESIS_NUM_PREDICT
    return min(cap, value + reserve)


def _metadata(generator: Any) -> Mapping[str, Any]:
    # Accept a raw provider response as well as a generator.  The durable
    # fanout synthesis path has the response dictionary directly and should
    # not need to manufacture a callable merely to preserve done_reason.
    if isinstance(generator, Mapping):
        return generator
    # ``_TierGenerator`` forwards this property to the bridged provider.  The
    # additional names keep this adapter useful for small test doubles and for
    # wrappers which deliberately expose response metadata under a neutral
    # name, without inspecting provider response bodies.
    for name in ("last_response_meta", "response_metadata", "metadata"):
        value = getattr(generator, name, None)
        if isinstance(value, Mapping):
            return value
    return {}


def response_was_length_limited(generator: Any) -> bool:
    """Whether sanitized provider metadata says output ended due to length."""
    metadata = _metadata(generator)
    for key in ("done_reason", "finish_reason"):
        reason = metadata.get(key)
        if isinstance(reason, str) and reason.strip().casefold() == "length":
            return True
    return False


def append_truncation_marker(text: str, *, generator: Any) -> str:
    """Append an explicit marker only for a provider-reported length stop."""
    result = str(text or "")
    if not response_was_length_limited(generator):
        return result
    if SUMMARY_TRUNCATED_MARKER in result:
        return result
    separator = "\n\n" if result else ""
    return result + separator + SUMMARY_TRUNCATED_MARKER


def generate_synthesis(
    generator: Callable[[str], str], prompt: str,
) -> str:
    """Call a generator already configured with its bounded output budget."""
    return append_truncation_marker(generator(prompt), generator=generator)


__all__ = [
    "BRIDGED_REASONING_RESERVE",
    "DEFAULT_SYNTHESIS_NUM_PREDICT",
    "MAX_BRIDGED_SYNTHESIS_NUM_PREDICT",
    "SUMMARY_TRUNCATED_MARKER",
    "append_truncation_marker",
    "generate_synthesis",
    "json_num_predict",
    "response_was_length_limited",
]
