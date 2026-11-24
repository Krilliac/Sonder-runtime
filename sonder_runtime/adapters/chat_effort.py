"""Resolve a chat turn's reasoning effort and propagate it across work runs.

Order: request > session (``/effort`` / UI selector) > lane (``SONDER_REASONING_EFFORT``) > preset (unset).
Uses a :class:`contextvars.ContextVar` so routed HTTP work runs spawned via
``contextvars.copy_context()`` inherit the active turn's effort automatically.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from ..application.session.effort_settings import STORE, effort_command, session_key
from ..domain.common.errors import InvalidInput
from ..domain.reasoning_effort import (
    EFFORT_LEVELS,
    ResolvedEffort,
    check_effort,
    format_effort,
    parse_effort_setting,
    resolve_effort,
)
from .chat_temperature import http_temperature_owner as _http_owner

logger = logging.getLogger(__name__)

_ACTIVE_EFFORT: ContextVar[str | None] = ContextVar("sonder_active_reasoning_effort", default=None)


def lane_default_effort() -> str | None:
    """Optional operator default from ``SONDER_REASONING_EFFORT``."""
    raw = os.environ.get("SONDER_REASONING_EFFORT", "").strip()
    if not raw:
        return None
    try:
        return check_effort(raw)
    except InvalidInput:
        logger.warning("SONDER_REASONING_EFFORT has malformed value=%r; using auto", raw)
        return None


def resolve_turn_effort(
    *, request: object = None, session: object = None, lane: object = None,
) -> ResolvedEffort:
    """Resolve effective reasoning effort for a chat or work turn."""
    effective_lane = lane_default_effort() if lane is None else lane
    resolved = resolve_effort(
        request=request,
        session=STORE.get(session),
        lane=effective_lane,
    )
    logger.debug("chat turn %s", resolved.describe())
    return resolved


def active_turn_effort() -> str | None:
    """The reasoning effort bound to the current turn/work-run context, or None."""
    return _ACTIVE_EFFORT.get()


@contextmanager
def effort_scope(value: object) -> Iterator[str | None]:
    """Bind a resolved reasoning effort for this turn and any copied worker contexts."""
    checked = check_effort(value)
    token = _ACTIVE_EFFORT.set(checked)
    try:
        yield checked
    finally:
        _ACTIVE_EFFORT.reset(token)


def session_field(raw: object, session: object) -> tuple[bool, str | None]:
    """Validate a chat body's ``session_reasoning_effort`` (the UI selector).

    Returns ``(apply, value)``: absent is ``(False, None)``; a valid level sets
    it; ``"auto"``/``"default"`` returns ``(True, None)`` and clears it.
    """
    if raw is None:
        return False, None
    value = parse_effort_setting(raw) if isinstance(raw, str) else check_effort(raw)
    if session_key(session) is None:
        raise InvalidInput("needs a session id in the request's session field")
    return True, value


def store_session_field(
    apply: bool,
    value: str | None,
    session: object,
    context: dict[str, Any] | None,
    principal_fn: Callable[[dict[str, Any]], str],
) -> None:
    if apply:
        STORE.set(session, value, owner=_http_owner(context, principal_fn))


def slash_effort(
    arg: object,
    session: object,
    context: dict[str, Any] | None,
    principal_fn: Callable[[dict[str, Any]], str],
    lane: object = None,
) -> str:
    return effort_command(
        arg,
        session,
        lane=lane_default_effort() if lane is None else lane,
        owner=_http_owner(context, principal_fn),
    )


__all__ = [
    "EFFORT_LEVELS",
    "active_turn_effort",
    "effort_command",
    "effort_scope",
    "format_effort",
    "lane_default_effort",
    "resolve_turn_effort",
    "session_field",
    "slash_effort",
    "store_session_field",
]
