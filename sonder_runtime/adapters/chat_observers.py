"""Best-effort observer hooks of the chat lanes.

``server.py`` is a size-capped control-plane file; its chat lanes report the
resolved target to an optional in-process observer through this one call.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def notify(observer: Callable[..., Any] | None, *args: Any) -> None:
    """Call ``observer(*args)`` when one is set; a failing observer never fails the turn."""
    if observer is None:
        return
    try:
        observer(*args)
    except Exception:  # noqa: BLE001, S110  # moved unchanged from server.py
        # Observability must not make a valid model call fail.  The
        # observer is an in-process metadata hook, never model input.
        pass


__all__ = ["notify"]
