"""Request-local narration callbacks; event stores remain the source of progress."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable
import uuid

_PROCESS = uuid.uuid4().hex + ":"


@dataclass
class WorkNarration:
    run_id: str = ""
    acknowledgement: str = ""
    activity_id: str = ""
    link_callback: Callable[[str, str], None] | None = None
    links: list[dict] = field(default_factory=list)
    outcome: str = "returned"


_CURRENT: ContextVar[WorkNarration | None] = ContextVar("work_narration", default=None)
_WRITER: ContextVar[Callable[[str], None] | None] = ContextVar("work_narration_writer", default=None)
_REASON: ContextVar[str] = ContextVar("work_narration_reason", default="")


def current() -> WorkNarration | None:
    return _CURRENT.get()


def writer() -> Callable[[str], None] | None:
    return _WRITER.get()


def qualify_activity_id(identity: str) -> str:
    return _PROCESS + identity if identity else ""


def local_activity_id(identity: str) -> str:
    return identity[len(_PROCESS):] if identity.startswith(_PROCESS) else ""


def bind_activity(identity: str) -> None:
    state = current()
    if state is not None:
        state.activity_id = qualify_activity_id(identity)
        link("activity", state.activity_id)


def route_reason(value: str = "") -> str:
    if value:
        if current() is not None or writer() is not None:
            _REASON.set(value)
        return value
    reason = _REASON.get()
    _REASON.set("")
    return reason


@contextmanager
def scope(run_id="", link_callback=None, acknowledgement="", activity_id=""):
    state = WorkNarration(run_id, acknowledgement, activity_id, link_callback)
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)
        _REASON.set("")


@contextmanager
def output(write):
    token = _WRITER.set(write)
    try:
        yield
    finally:
        _WRITER.reset(token)


def link(kind: str, run_id: str) -> None:
    """Bind only host-created ids, never ids extracted from model prose."""
    state = current()
    item = {"kind": kind, "id": run_id}
    if state is None or not run_id or item in state.links or len(state.links) >= 16:
        return
    if state.link_callback is not None:
        state.link_callback(kind, run_id)
    state.links.append(item)
