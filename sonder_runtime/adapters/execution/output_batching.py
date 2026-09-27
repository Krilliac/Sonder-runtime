"""Group-commit window for process output on its way to the durable registry.

Reader threads hand each line they read to an ``OutputBatcher``; one persister
thread per job takes whatever has accumulated and publishes it through a single
registry call (one SQLite transaction).  A batch is flushed as soon as any of
these holds:

- it holds ``max_lines`` lines or ``max_bytes`` UTF-8 bytes;
- its oldest line has waited ``max_delay_seconds`` (a quiet child still sees
  its output published within that bound);
- every reader has reached end of file (the child exited or closed its pipes);
- a caller asked for a flush (cancellation, ``wait``).

Unpersisted output is bounded, which is also the crash-loss bound: readers
block while the pending batch plus the batch being committed holds
``max_lines`` lines or ``max_bytes`` bytes.  So at most ``max_lines`` lines
and ``max_bytes`` bytes (plus the one line that crossed the byte bound) are
queued, and each reader blocked in ``put`` holds one more line it has already
read.  Read-but-not-durable output is therefore at most that window plus one
line per reader (two: stdout and stderr).  A crash of the runtime loses at
most that; every earlier line is already committed, and because each batch
commits atomically and in order the registry always holds an exact, gap-free
prefix of each stream as read.

Lines are never merged: each keeps its own output sequence number, stream and
exact text, so retention, watermarks and the output-limit accounting see the
same events as the one-commit-per-line path they replace.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Sequence

from ...application.execution.world_control import OutputStream
from ...application.jobs.durable_registry import MAX_OUTPUT_APPEND_BATCH


# Defaults chosen from measurement (see
# docs/architecture/evidence/PROCESS-OUTPUT-BATCHING-2026-09-26.md).  A registry
# commit costs about the same whatever it carries: on Linux ~1.5 ms for one
# line and ~2.5-4 ms for 1024 lines; on a hosted Windows runner a commit is
# ~60 ms.  1024 lines therefore cuts the per-line cost by two to three orders
# of magnitude while one transaction stays well under the registry's
# 4096-entry cap.  256 KiB bounds reader memory and the crash window for long
# lines (a 4000-byte-line flood commits ~65 lines at a time), and 50 ms keeps
# live output visibly incremental when the child goes quiet.
OUTPUT_BATCH_MAX_LINES = 1024
OUTPUT_BATCH_MAX_BYTES = 256 * 1024
OUTPUT_BATCH_MAX_DELAY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class OutputBatchPolicy:
    """Bounds of one output persistence window (whichever is reached first)."""

    max_lines: int = OUTPUT_BATCH_MAX_LINES
    max_bytes: int = OUTPUT_BATCH_MAX_BYTES
    max_delay_seconds: float = OUTPUT_BATCH_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_lines, bool)
            or not isinstance(self.max_lines, int)
            or not 1 <= self.max_lines <= MAX_OUTPUT_APPEND_BATCH
        ):
            raise ValueError(f"max_lines must be within 1..{MAX_OUTPUT_APPEND_BATCH}")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or not 1 <= self.max_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("max_bytes must be within 1..64 MiB")
        if (
            isinstance(self.max_delay_seconds, bool)
            or not isinstance(self.max_delay_seconds, (int, float))
            or not 0 < self.max_delay_seconds <= 60
        ):
            raise ValueError("max_delay_seconds must be within (0, 60]")


class OutputBatcher:
    """Bounded hand-off between a job's pipe readers and its persister.

    ``put`` (reader side) blocks while the unpersisted window is full, which
    applies the same backpressure to the child that a slow per-line commit
    did.  ``run`` (persister side) returns when every reader has finished and
    the last batch is committed, or when ``persist`` raised; after a failure
    ``put`` returns ``False`` so readers stop, as they did when their own
    commit failed.
    """

    def __init__(
        self,
        persist: Callable[[Sequence[tuple[OutputStream, str]]], None],
        *,
        writers: int,
        policy: OutputBatchPolicy | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(persist):
            raise TypeError("persist must be callable")
        if isinstance(writers, bool) or not isinstance(writers, int) or writers < 1:
            raise ValueError("writers must be a positive integer")
        self._persist = persist
        self._policy = policy or OutputBatchPolicy()
        self._on_failure = on_failure
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._pending: list[tuple[OutputStream, str]] = []
        self._pending_bytes = 0
        self._inflight_lines = 0
        self._inflight_bytes = 0
        self._first_at: float | None = None
        self._writers = writers
        self._flush_requested = 0
        self._flush_done = 0
        self._stopped = False
        self._failed = False
        self._finished = False

    @property
    def policy(self) -> OutputBatchPolicy:
        return self._policy

    # -- reader side -----------------------------------------------------------

    def put(self, stream: OutputStream, data: str) -> bool:
        """Queue one line; ``False`` once persistence has stopped."""
        size = len(data.encode("utf-8"))
        policy = self._policy
        with self._cond:
            while not self._stopped:
                lines = len(self._pending) + self._inflight_lines
                used = self._pending_bytes + self._inflight_bytes
                if lines == 0 or (lines < policy.max_lines and used < policy.max_bytes):
                    break
                self._cond.wait()
            if self._stopped:
                return False
            if not self._pending:
                self._first_at = self._clock()
            self._pending.append((stream, data))
            self._pending_bytes += size
            self._cond.notify_all()
            return True

    def writer_done(self) -> None:
        """One reader reached end of file (or stopped)."""
        with self._cond:
            self._writers = max(0, self._writers - 1)
            self._cond.notify_all()

    # -- control ---------------------------------------------------------------

    def request_flush(self) -> int:
        """Ask the persister to publish what is pending now; never blocks."""
        with self._cond:
            self._flush_requested += 1
            self._cond.notify_all()
            return self._flush_requested

    def flush(self, timeout: float) -> bool:
        """Publish everything queued before this call; ``True`` when durable.

        Returns ``False`` on timeout or when persistence stopped on a failure.
        """
        deadline = self._clock() + max(0.0, float(timeout))
        with self._cond:
            self._flush_requested += 1
            ticket = self._flush_requested
            self._cond.notify_all()
            while self._flush_done < ticket and not self._finished:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return self._flush_done >= ticket or (self._finished and not self._failed)

    @property
    def finished(self) -> bool:
        with self._cond:
            return self._finished

    # -- persister side --------------------------------------------------------

    def _due(self, now: float) -> bool:
        policy = self._policy
        return bool(self._pending) and (
            len(self._pending) >= policy.max_lines
            or self._pending_bytes >= policy.max_bytes
            or self._writers == 0
            or self._flush_requested > self._flush_done
            or (self._first_at is not None and now - self._first_at >= policy.max_delay_seconds)
        )

    def run(self) -> None:
        """Persister loop; the owning thread's target."""
        try:
            while True:
                with self._cond:
                    while True:
                        now = self._clock()
                        if self._due(now):
                            break
                        if not self._pending:
                            if self._flush_requested > self._flush_done:
                                self._flush_done = self._flush_requested
                                self._cond.notify_all()
                            if self._writers == 0:
                                return
                            self._cond.wait()
                            continue
                        first_at = now if self._first_at is None else self._first_at
                        self._cond.wait(
                            max(0.0, first_at + self._policy.max_delay_seconds - now)
                        )
                    batch = self._pending
                    ticket = self._flush_requested
                    self._pending = []
                    self._inflight_lines = len(batch)
                    self._inflight_bytes = self._pending_bytes
                    self._pending_bytes = 0
                    self._first_at = None
                try:
                    self._persist(tuple(batch))
                except BaseException as exc:
                    with self._cond:
                        self._stopped = True
                        self._failed = True
                        self._pending = []
                        self._pending_bytes = 0
                        self._cond.notify_all()
                    if self._on_failure is not None and isinstance(exc, Exception):
                        self._on_failure(exc)
                    if not isinstance(exc, Exception):
                        raise
                    return
                with self._cond:
                    self._inflight_lines = 0
                    self._inflight_bytes = 0
                    self._flush_done = max(self._flush_done, ticket)
                    self._cond.notify_all()
        finally:
            with self._cond:
                self._stopped = True
                self._finished = True
                self._cond.notify_all()


__all__ = [
    "OUTPUT_BATCH_MAX_BYTES", "OUTPUT_BATCH_MAX_DELAY_SECONDS", "OUTPUT_BATCH_MAX_LINES",
    "OutputBatchPolicy", "OutputBatcher",
]
