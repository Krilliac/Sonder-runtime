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
import importlib
import time
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


def fleet_fence(agent_id: str, owner_id: str) -> Fence:
    """The fence for one fleet worker: the agent row it runs stays its owner's."""
    from ..persistence import fleet_store

    agent_id = str(agent_id or "")
    owner_id = str(owner_id or "")

    def check() -> str:
        row = fleet_store.get_agent(agent_id)
        if row is None or str(row.get("owner_id") or "") != owner_id:
            return (
                "fleet agent %s is no longer owned by this worker "
                "(owner heartbeat expired or the agent was reassigned)" % agent_id
            )
        if row.get("cancel_requested") or row.get("status") in ("cancelled", "interrupted"):
            return "fleet agent %s was cancelled" % agent_id
        if row.get("status") not in ("queued", "running"):
            return "fleet agent %s is %s" % (agent_id, row.get("status"))
        return ""

    return Fence("fleet:%s" % agent_id, check)


def selfmod_fence(run_id: str, owner_id: str) -> Fence:
    """The fence for the selfmod editing worker: its lease on the run."""
    run_id = str(run_id or "")
    owner_id = str(owner_id or "")

    def check() -> str:
        selfmod = importlib.import_module("selfmod")  # root module, resolved lazily
        try:
            run = selfmod.get_run(run_id)
        except KeyError:
            return "selfmod run %s no longer exists" % run_id
        if str(run.get("owner_id") or "") != owner_id:
            return "selfmod run %s is no longer owned by this worker" % run_id
        if float(run.get("lease_until") or 0) < time.time():
            return "the lease on selfmod run %s expired" % run_id
        if run.get("phase") not in ("editing", "testing", "reviewing"):
            return "selfmod run %s is %s" % (run_id, run.get("phase"))
        return ""

    return Fence("selfmod:%s" % run_id, check)


__all__ = [
    "Fence", "autopilot_fence", "current", "fleet_fence", "held", "reason_lost",
    "selfmod_fence",
]
