"""Deployment authentication policy independent of the serving transport."""

from __future__ import annotations

import os


_AFFIRMATIVE = ("1", "true", "yes", "on")


def authenticates_callers(*, environ=None) -> bool:
    """Return whether the deployment distinguishes more than one caller.

    A configured auth mode, API key, or required account makes caller-owned
    data security-relevant. With none of those settings, the runtime is the
    single-operator local-open deployment. ``environ`` is injectable so the
    policy is deterministic for tests and embedders.
    """
    values = os.environ if environ is None else environ
    if values.get("SONDER_AUTH_MODE", "").strip():
        return True
    if values.get("SONDER_API_KEY", "").strip():
        return True
    return values.get("SONDER_REQUIRE_ACCOUNT", "").strip().lower() in _AFFIRMATIVE
