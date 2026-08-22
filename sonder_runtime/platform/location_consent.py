"""Environment-backed privacy policy for approximate location lookup."""

from __future__ import annotations

import os


def location_consent(*, environ=None) -> bool:
    """Return whether the host explicitly opts in to location lookup.

    Approximate-IP location is disabled unless the environment contains one
    of the historical affirmative values. ``environ`` is injectable so the
    policy remains deterministic for callers and tests.
    """
    values = os.environ if environ is None else environ
    return values.get("SONDER_LOCATION_CONSENT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
