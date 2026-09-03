"""Classify whether an agent tool observation indicates success or failure."""

from __future__ import annotations


def observation_ok(observation) -> bool:
    """Return False when the observation text signals a failed tool step."""
    text = str(observation or "")
    lowered = text.lower()
    first = next(
        (line.strip().lower() for line in text.splitlines() if line.strip()), ""
    )
    return not (
        text.startswith("ERROR:")
        or "  ok: false" in lowered
        or first.endswith(": fail")
        or first.startswith("validation_failed")
        or "[fail]" in lowered
    )
