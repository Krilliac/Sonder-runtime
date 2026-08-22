"""Context-size selection adapter used by runtime callers."""

import sonder_runtime.platform.context_policy as context_policy


def requested_context(value=None, *, default_value=None) -> int:
    """Resolve a caller value against the configured virtual context ceiling."""
    fallback = (
        context_policy.default_requested()
        if default_value is None
        else default_value
    )
    selected = fallback if value in (None, "") else value
    return context_policy.requested(selected)


def native_context(value=None, *, default_value=None) -> int:
    """Resolve a caller value to the backend's native context ceiling."""
    return context_policy.native(
        requested_context(value, default_value=default_value)
    )
