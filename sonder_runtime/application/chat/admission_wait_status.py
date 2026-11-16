"""Visible status while a model load waits in GPU admission.

A streamed chat turn commits to SSE before the model call, so a load that sits
in the GPU admission queue (host memory below the floor, a busy slot) used to
look hung: the client saw keep-alives only, then one error after the whole
wait.  The streaming handler arms a *sink* for the turn; admission reports each
wait iteration through :func:`report_wait`, which rate-limits and forwards one
human-readable line per interval to the sink.

State and threads: the armed sink lives in a ``ContextVar`` (the same carrier
the live token stream uses), so it follows the request's thread and context and
is invisible to other requests.  ``AdmissionWaitReporter`` is not thread-safe;
one is created per armed turn and used from the thread that waits.

Application module: stdlib only.
"""
from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from typing import Callable, Iterator

DEFAULT_INTERVAL_SECONDS = 15.0
_GIB = 1024.0 ** 3


def describe_wait(model: str, free_bytes: int | None, needed_bytes: int | None,
                  detail: str = "") -> str:
    """One line a person can read, with the numbers when they are known."""
    name = model or "the model"
    if free_bytes is not None and needed_bytes is not None:
        return "waiting for memory to load %s: %.1f GiB free, needs ~%.1f GiB" % (
            name, free_bytes / _GIB, needed_bytes / _GIB)
    if detail:
        return "waiting to load %s: %s" % (name, detail)
    return "waiting to load %s: the GPU queue is busy" % name


class AdmissionWaitReporter:
    """Forwards at most one status line per ``interval_seconds`` to ``emit``.

    ``emit`` returns normally or raises; a raising sink is dropped for the rest
    of the turn so a gone client never breaks admission.
    """

    def __init__(self, emit: Callable[[str], None], *,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._emit = emit
        self._interval = float(interval_seconds)
        self._clock = clock
        self._last: float | None = None
        self._dead = False
        self.waiting = False

    def report(self, line: str) -> bool:
        """Emit ``line`` when the interval has passed; ``True`` if emitted."""
        self.waiting = True
        if self._dead:
            return False
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        try:
            self._emit(line)
        except Exception:
            self._dead = True
            return False
        return True

    def admitted(self) -> None:
        """The wait is over; later waits of the same turn start a fresh cadence."""
        self.waiting = False
        self._last = None


_REPORTER: ContextVar[AdmissionWaitReporter | None] = ContextVar(
    "sonder_admission_wait_reporter", default=None)


@contextlib.contextmanager
def armed(reporter: AdmissionWaitReporter | None) -> Iterator[AdmissionWaitReporter | None]:
    """Arm ``reporter`` for the enclosed turn (``None`` arms nothing)."""
    token = _REPORTER.set(reporter)
    try:
        yield reporter
    finally:
        _REPORTER.reset(token)


def report_wait(model: str, free_bytes: int | None = None, needed_bytes: int | None = None,
                detail: str = "") -> bool:
    """Called by admission on every queued iteration; a no-op unless armed."""
    reporter = _REPORTER.get()
    if reporter is None:
        return False
    return reporter.report(describe_wait(model, free_bytes, needed_bytes, detail))


def report_admitted() -> None:
    """Called by admission once the request is admitted; a no-op unless armed."""
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter.admitted()


__all__ = [
    "AdmissionWaitReporter", "DEFAULT_INTERVAL_SECONDS", "armed", "describe_wait",
    "report_admitted", "report_wait",
]
