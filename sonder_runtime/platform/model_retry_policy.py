"""Environment-backed retry eligibility policy for model requests."""

from __future__ import annotations

import os


def hosted_overflow_retry_enabled() -> bool:
    """Return whether the operator explicitly enabled hosted overflow retries."""
    return os.environ.get("SONDER_HOSTED_OVERFLOW_RETRY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def overflow_retry_allowed(*, cloud: bool, remote: bool, idempotent: bool) -> bool:
    """Return whether a compacted model request may spend one extra attempt.

    Loopback Ollama is free and side-effect-free, so it may retry. Hosted and
    remote routes require both an idempotent request and explicit operator
    opt-in because a retry can duplicate metered work or side effects.
    """
    if not (cloud or remote):
        return True
    return bool(idempotent) and hosted_overflow_retry_enabled()
