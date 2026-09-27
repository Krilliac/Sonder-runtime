"""Bounded queue of log notices the interactive REPL shows between turns.

Interactive REPL logs go to a file (``SONDER_HOME/logs/repl.log``); records at
WARNING and above are also pushed here so the REPL can show one muted line
(``! 2 notices · /logs``) between turns instead of JSON on the terminal.

The entry point (``__main__.cmd_repl``) creates the queue, hands its ``push``
method to the platform ``NoticeQueueHandler`` and calls
:func:`install_repl_notices`. Interfaces read it through the functions below
and never import the platform logging module.

REPL integration API (the only calls the REPL needs)::

    from sonder_runtime.application.ports import repl_notices

    # Between turns, never while the live line is drawn:
    notices = repl_notices.drain_repl_notices()      # tuple[ReplNotice, ...]
    dropped = repl_notices.last_drain_dropped()      # records lost to the bound
    # For /logs [n]:
    path = repl_notices.repl_log_path()              # str | None

``drain_repl_notices()`` returns ``()`` when no queue is installed (piped
stdin/stdout, ``--json``, ``SONDER_REPL_LOG_STDERR=1``, or tests), so the
caller needs no guard.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
import unicodedata

DEFAULT_CAPACITY = 50
_MESSAGE_LIMIT = 500

# Numeric levels, copied so this port does not depend on ``logging`` config.
WARNING = 30
ERROR = 40


@dataclass(frozen=True)
class ReplNotice:
    """One redacted log record, reduced to what a terminal line needs."""

    level: int
    levelname: str
    component: str
    message: str  # redacted, terminal-safe (controls escaped), <= 500 chars
    created: float

    @property
    def is_error(self) -> bool:
        return self.level >= ERROR


# Kept as-is; every other control, format (bidi override, isolate, zero
# width), surrogate or line/paragraph separator code point is shown as an
# escape so a log message can never drive the terminal it is drawn on.
_KEEP = frozenset("\n\t")
_ESCAPED_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))


def _terminal_safe(text: str) -> str:
    """Escape C0/C1 controls, DEL and Cf characters as ``\\xNN``/``\\uNNNN``."""
    out = []
    for ch in text:
        if ch in _KEEP or unicodedata.category(ch) not in _ESCAPED_CATEGORIES:
            out.append(ch)
        elif ord(ch) < 0x100:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append("\\u%04x" % ord(ch))
    return "".join(out)


class ReplNoticeQueue:
    """Thread-safe, bounded FIFO. The oldest record is dropped when full."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("notice queue capacity must be a positive int")
        self._items: deque[ReplNotice] = deque(maxlen=capacity)
        self._lock = Lock()
        self._dropped = 0
        self._last_dropped = 0
        self.capacity = capacity

    def push(
        self,
        level: int,
        levelname: str,
        component: str,
        message: str,
        created: float,
    ) -> None:
        text = _terminal_safe(str(message or ""))
        if len(text) > _MESSAGE_LIMIT:
            text = text[: _MESSAGE_LIMIT - 3] + "..."
        notice = ReplNotice(
            int(level),
            _terminal_safe(str(levelname)),
            _terminal_safe(str(component or "")),
            text,
            float(created),
        )
        with self._lock:
            if len(self._items) == self._items.maxlen:
                self._dropped += 1
            self._items.append(notice)

    def drain(self, limit: int | None = None) -> tuple[ReplNotice, ...]:
        with self._lock:
            count = len(self._items) if limit is None else max(0, min(limit, len(self._items)))
            taken = tuple(self._items.popleft() for _ in range(count))
            self._last_dropped = self._dropped
            self._dropped = 0
            return taken

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def last_dropped(self) -> int:
        return self._last_dropped


_INSTALL_LOCK = Lock()
_QUEUE: ReplNoticeQueue | None = None
_LOG_PATH: str | None = None


def install_repl_notices(queue: ReplNoticeQueue | None, *, log_path: str | None = None) -> None:
    """Bind the process queue and log file path (entry point only)."""
    global _QUEUE, _LOG_PATH
    if queue is not None and not isinstance(queue, ReplNoticeQueue):
        raise TypeError("ReplNoticeQueue required")
    with _INSTALL_LOCK:
        _QUEUE = queue
        _LOG_PATH = str(log_path) if log_path else None


def reset_repl_notices() -> None:
    install_repl_notices(None)


def drain_repl_notices(limit: int | None = None) -> tuple[ReplNotice, ...]:
    """Remove and return queued notices, oldest first; ``()`` if none."""
    queue = _QUEUE
    return () if queue is None else queue.drain(limit)


def pending_repl_notices() -> int:
    queue = _QUEUE
    return 0 if queue is None else queue.pending()


def last_drain_dropped() -> int:
    """Records discarded by the bound before the most recent drain."""
    queue = _QUEUE
    return 0 if queue is None else queue.last_dropped


def repl_log_path() -> str | None:
    """Path of the REPL log file, or ``None`` when logs go to stderr."""
    return _LOG_PATH


__all__ = [
    "DEFAULT_CAPACITY",
    "ReplNotice",
    "ReplNoticeQueue",
    "drain_repl_notices",
    "install_repl_notices",
    "last_drain_dropped",
    "pending_repl_notices",
    "repl_log_path",
    "reset_repl_notices",
]
