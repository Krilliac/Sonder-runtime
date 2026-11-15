"""Which tool, if any, a person at the console typed for the call running now.

The live runtime checkout (``live_checkout``) may be changed by a git or patch
tool, and ``runtime_source_stash pop`` may write into it, only when the
operator typed that very tool at the console.  The console wraps the handler
in :func:`attended` while a person is present: the ``/<tool>`` dispatcher
(``repl._run_catalogued``) and the console commands that front one such tool
(``repl._console_attendance``, e.g. ``/stash``).  Nothing else sets it.

The signal is bound to one tool name, not a flag, and that binding is what
makes it safe.  A person who types ``/agent`` runs the model loop in the same
thread, and every git call the model makes there must still read as
unattended; it does, because the signal names ``agent``.  The context does
follow work handed on from inside an attended call: ``asyncio`` tasks and
``asyncio.to_thread`` copy it, and so does a ``threading.Thread`` started
while Python 3.14's ``thread_inherit_context`` flag is on.  Such work still
holds only the one tool name the operator typed, so it can do no more than
that tool's own call; a thread started with the flag off (the default on the
standard build) begins with an empty context and is unattended.

State: one ``ContextVar`` holding a tool name or ``None``; per thread / task,
never shared, nothing to lock.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ATTENDED: ContextVar[str | None] = ContextVar("sonder_operator_attended_tool", default=None)


@contextmanager
def attended(tool_name: str) -> Iterator[None]:
    """Mark the calls made inside the block as typed by the operator for ``tool_name`` only."""
    token = _ATTENDED.set(tool_name)
    try:
        yield
    finally:
        _ATTENDED.reset(token)


@contextmanager
def unattended() -> Iterator[None]:
    """Clear the signal for the block (a nested caller that is not the operator)."""
    token = _ATTENDED.set(None)
    try:
        yield
    finally:
        _ATTENDED.reset(token)


def attended_for(tool_name: str) -> bool:
    """Whether the operator typed exactly ``tool_name`` for the call running now."""
    return _ATTENDED.get() == tool_name


__all__ = ["attended", "attended_for", "unattended"]
