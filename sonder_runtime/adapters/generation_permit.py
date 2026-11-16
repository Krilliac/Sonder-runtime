"""A turn-bound permit for one restricted agent turn's own model calls.

A model a restricted profile matches stays on the tool-lane deny list, so the
generation guard (``tool_lane_guard.require_generation``) refuses every call
to it.  A restricted agent turn wraps only its own generator with
:func:`wrap`; for the length of each call to that generator, on the thread
making it, the guard may let exactly that model through (:func:`permits`),
after the turn's own check (``still_granted``) decides the grant again.

Ownership and threads.  The permit lives in a private context variable that
:func:`wrap` sets before the call and resets after it, so a call made on
another thread, after the call returns, or from a tool dispatch between
generator calls does not carry it.  The permit names one model, so it opens
nothing else.  This module imports nothing from the guard or the lane, so
both can import it without a cycle; it does no I/O.
"""
from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sonder_runtime.domain.tool_lane_models import normalize_name


@dataclass(frozen=True)
class _Permit:
    model: str  # normalised
    still_granted: Callable[[Mapping[str, Any] | None], bool]


_CURRENT: ContextVar[_Permit | None] = ContextVar("sonder_generation_permit", default=None)


def wrap(
    generate: Callable[..., Any], *, model: str,
    still_granted: Callable[[Mapping[str, Any] | None], bool],
) -> Callable[..., Any]:
    """``generate``, with ``model`` permitted for the length of each call on the calling thread."""
    permit = _Permit(normalize_name(model), still_granted)
    if not permit.model:
        raise ValueError("a generation permit needs a model name")

    @functools.wraps(generate)
    def generate_permitted(*args: Any, **kwargs: Any) -> Any:
        token = _CURRENT.set(permit)
        try:
            return generate(*args, **kwargs)
        finally:
            _CURRENT.reset(token)

    return generate_permitted


def permits(model: Any, policy: Mapping[str, Any] | None = None) -> bool:
    """Whether a denied ``model`` is the permitted model of the call running now, still granted.

    False outside a :func:`wrap` call, for any other model, and on any fault
    in the grant check.
    """
    permit = _CURRENT.get()
    if permit is None or normalize_name(model) != permit.model:
        return False
    try:
        return permit.still_granted(policy) is True
    except Exception:  # noqa: BLE001 - a fault grants nothing
        return False


__all__ = ["permits", "wrap"]
