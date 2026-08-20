"""Pure cancellation safety policy shared by bounded runtime operations."""

from __future__ import annotations


def cancellation_requested(cancel_check) -> bool:
    """Return whether a cancellation callback requests stopping the operation.

    A failing cancellation probe is treated as a request to stop. This is a
    safety gate: an unreadable durable cancellation state must not authorize
    another potentially expensive model request.
    """
    if cancel_check is None:
        return False
    try:
        return bool(cancel_check())
    except Exception:
        return True
