"""Cancellation scopes for interactive foreground turns.

An interactive surface (the REPL) runs each submitted turn inside a child
scope of one process-wide :class:`CancellationTree`.  When the operator
interrupts a turn (Ctrl-C), the surface cancels that turn's node; the request
propagates to every descendant scope, and cooperative checkpoints -- the model
request boundary and the agent loop's step boundary -- refuse further work for
the cancelled turn even when an intermediate layer swallowed the original
``KeyboardInterrupt``.

The current turn is tracked with a :class:`contextvars.ContextVar`, so work
that runs outside the turn's context (detached background fleets, autopilot
runs, HTTP handlers) is never cancelled by an unrelated foreground interrupt.
"""
from __future__ import annotations

import itertools
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from sonder_runtime.domain.cancellation_tree import CancellationNode

from .cancellation_tree import CancellationTree

INTERRUPT_REASON = "interrupted by the operator (Ctrl-C)"

_TREE = CancellationTree(root_id="foreground")
_IDS = itertools.count(1)
_CURRENT: ContextVar[CancellationNode | None] = ContextVar(
    "sonder_foreground_turn", default=None,
)
_LOG = logging.getLogger(__name__)
# Extra stop conditions bound by a surface that has no cancellation node of its
# own (an HTTP work run stops on its effect fence).  Immutable tuple: a nested
# scope rebinds a longer tuple and resets it, never mutates the outer one.
_CHECKS: ContextVar[tuple[Callable[[], object], ...]] = ContextVar(
    "sonder_foreground_checks", default=(),
)


@contextmanager
def foreground_turn(label: str = "turn") -> Iterator[CancellationNode]:
    """Run the body as one cancellable foreground turn.

    Nested turns become children of the enclosing turn, so cancelling an outer
    turn also cancels any inner scope.  The node is released from the tree when
    the body exits, whatever the outcome.
    """
    parent = _CURRENT.get()
    parent_id = parent.node_id if parent is not None else _TREE.root.node_id
    node = _TREE.create_child(parent_id, node_id="%s-%d" % (label or "turn", next(_IDS)))
    token = _CURRENT.set(node)
    lease = node.acquire()
    try:
        yield node
    finally:
        lease.release()
        _CURRENT.reset(token)
        _TREE.discard(node.node_id)


def current() -> CancellationNode | None:
    """Return the foreground turn scope active in this context, if any."""
    return _CURRENT.get()


@contextmanager
def check_scope(check: Callable[[], object]) -> Iterator[None]:
    """Bind ``check`` so :func:`cancel_requested` is also true while it is truthy.

    The check runs on the calling thread at every cooperative checkpoint, so it
    must be cheap.  A check that raises counts as a stop request: a run that
    cannot prove it may continue must not.
    """
    token = _CHECKS.set((*_CHECKS.get(), check))
    try:
        yield
    finally:
        _CHECKS.reset(token)


def cancel_requested() -> bool:
    """Return whether the foreground turn running in this context is cancelled.

    True when the turn's node is cancelled or any :func:`check_scope` check
    bound in this context is truthy.
    """
    node = _CURRENT.get()
    if node is not None and node.cancelled:
        return True
    for check in _CHECKS.get():
        try:
            if check():
                return True
        except Exception:
            _LOG.warning("foreground cancel check failed; treating it as a stop request", exc_info=True)
            return True
    return False


def cancel(node: CancellationNode | None, *, reason: str = INTERRUPT_REASON) -> bool:
    """Cancel one turn scope and its descendants; ``None`` is a no-op."""
    if node is None:
        return False
    return node.cancel(reason=reason)


__all__ = [
    "INTERRUPT_REASON",
    "cancel",
    "cancel_requested",
    "check_scope",
    "current",
    "foreground_turn",
]
