"""A fence on effects: the check the permission gate runs before any effect.

Autopilot already fences its *record*: every progress write is conditional on
the worker still owning the run's lease, so a worker that lost its lease
cannot overwrite a successor's run. The tool calls a task makes in between
those writes were not fenced: a worker whose lease expired mid-task kept
writing files and running programs until the next checkpoint noticed.

This module carries the fence to the effects. A worker installs one for the
duration of its task (``held``); ``permission_modes.decide`` asks the current
fence before allowing any tool of an effect class (file changes, host
programs, destructive tools) on that thread, and refuses -- ``source="fence"``,
with a receipt -- the moment the fence reports that it no longer holds. A
check that raises is treated as a lost fence: an effect whose authority cannot
be verified is not produced.

The fence is a context variable, so it is per thread and per task: the
autopilot worker thread sees its own fence, and nothing else in the process
sees one at all. Reads are never fenced; a worker that lost its lease may
still look, it may not touch.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass(frozen=True)
class Fence:
    """``check()`` returns "" while the fence holds, else why it no longer does."""

    label: str
    check: Callable[[], str]


_FENCE: contextvars.ContextVar[Fence | None] = contextvars.ContextVar(
    "sonder_effect_fence", default=None,
)


def current() -> Fence | None:
    """The fence on this thread's effects, if a worker installed one."""
    return _FENCE.get()


@contextlib.contextmanager
def held(fence: Fence) -> Iterator[Fence]:
    """Install ``fence`` for the body; the previous fence (if any) is restored."""
    token = _FENCE.set(fence)
    try:
        yield fence
    finally:
        _FENCE.reset(token)


def reason_lost(fence: Fence | None) -> str:
    """"" while the fence holds; the reason otherwise, a failed check included."""
    if fence is None:
        return ""
    try:
        return str(fence.check() or "")
    except Exception as exc:  # a fence that cannot be verified does not hold
        return "the fence %s could not be verified: %s" % (fence.label, exc)


def autopilot_fence(run_id: str, owner_id: str) -> Fence:
    """The fence for one autopilot worker: its lease on ``run_id`` as ``owner_id``."""
    from ..persistence import autopilot_store

    run_id = str(run_id or "")
    owner_id = str(owner_id or "")

    def check() -> str:
        flags = autopilot_store.control_flags(run_id, owner_id)
        if flags.get("lost"):
            return (
                "autopilot run %s is no longer owned by this worker "
                "(lease lost or taken over)" % run_id
            )
        if flags.get("cancel"):
            return "autopilot run %s was cancelled" % run_id
        return ""

    return Fence("autopilot:%s" % run_id, check)


__all__ = ["Fence", "autopilot_fence", "current", "held", "reason_lost"]
