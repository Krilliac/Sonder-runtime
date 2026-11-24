"""ContextVar-backed hosted-account boundary for task persistence and autonomous agent runs."""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

_TASK_ACCOUNT_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sonder_task_account_scope",
    default=None,
)


def active_task_account_scope() -> str | None:
    """Return the active hosted-account task/agent boundary, or ``None`` in operator mode."""
    return _TASK_ACCOUNT_SCOPE.get()


@contextlib.contextmanager
def scoped_task_account_boundary(account_scope: str | None) -> Iterator[str | None]:
    """Pin task persistence and autonomous agent tools to one hosted account.

    Fail-closed on nesting: once an account boundary is active, inner code
    cannot widen back to global (``None``) or switch to another account scope.
    """
    if account_scope is None:
        scope: str | None = None
    elif isinstance(account_scope, str):
        scope = account_scope.strip()
        if not scope:
            raise ValueError("task account scope must not be empty")
    else:
        raise ValueError("task account scope must be a string")
    current = _TASK_ACCOUNT_SCOPE.get()
    if current is not None:
        if scope is None or scope != current:
            raise PermissionError("cannot widen or switch an active account task scope")
        yield current
        return
    if scope is None:
        yield None
        return
    token = _TASK_ACCOUNT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _TASK_ACCOUNT_SCOPE.reset(token)


def hosted_account_scope_or_none(value: str | None) -> str | None:
    """Return *value* if it identifies a hosted HTTP account scope (``ta-...``)."""
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.startswith("ta-") and len(cleaned) > 3:
            return cleaned
    return None


@contextlib.contextmanager
def autopilot_account_boundary(request_owner: str | None) -> Iterator[str | None]:
    """Maintain or enter a hosted account scope for ``ta-...`` autopilot runs."""
    current = _TASK_ACCOUNT_SCOPE.get()
    hosted = hosted_account_scope_or_none(request_owner)
    if current is not None:
        if hosted is not None and hosted != current:
            raise PermissionError("cannot widen or switch an active account task scope")
        yield current
        return
    if hosted is None:
        yield None
        return
    with scoped_task_account_boundary(hosted) as active:
        yield active


__all__ = [
    "active_task_account_scope",
    "autopilot_account_boundary",
    "hosted_account_scope_or_none",
    "scoped_task_account_boundary",
]
