"""Graceful shutdown coordination (SPEC-2 section 12).

On SIGTERM/SIGINT or an explicit drain request the coordinator moves the
service state to DRAINING, rejects new mutating work, signals cancellation
to foreground requests, waits for active mutations to reach a safe point,
runs the registered flush hooks, and finally reports STOPPING.  The drain
deadline must stay below the service manager's kill timeout so systemd
never has to SIGKILL a healthy drain.
"""
from __future__ import annotations

import signal
import threading
import time

from sonder_service_state import ProcessState, ServiceStateTracker


class CancellationToken:
    """Cooperative cancellation shared by a request or drain scope."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class ShutdownCoordinator:
    def __init__(
        self,
        tracker: ServiceStateTracker,
        *,
        drain_deadline_seconds: float = 25.0,
    ) -> None:
        self._tracker = tracker
        self._deadline = drain_deadline_seconds
        self._lock = threading.Lock()
        self._active_mutations = 0
        self._idle = threading.Condition(self._lock)
        self._draining = threading.Event()
        self._cancellation = CancellationToken()
        self._flush_hooks: list = []
        self._interrupted_hooks: list = []

    # -- work admission ----------------------------------------------------

    def begin_mutation(self) -> bool:
        """Reserve a mutation slot; False once draining has begun."""
        with self._lock:
            if self._draining.is_set():
                return False
            self._active_mutations += 1
            return True

    def end_mutation(self) -> None:
        with self._idle:
            self._active_mutations = max(0, self._active_mutations - 1)
            if self._active_mutations == 0:
                self._idle.notify_all()

    @property
    def active_mutations(self) -> int:
        with self._lock:
            return self._active_mutations

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    @property
    def cancellation(self) -> CancellationToken:
        return self._cancellation

    # -- hooks -------------------------------------------------------------

    def add_flush_hook(self, hook) -> None:
        """Called after drain completes: flush logs, events, close stores."""
        self._flush_hooks.append(hook)

    def add_interrupted_hook(self, hook) -> None:
        """Called when the deadline expires with mutations still active.

        Durable-task owners use this to mark unfinished ownership as
        interrupted instead of leaving it silently claimed.
        """
        self._interrupted_hooks.append(hook)

    # -- signals -----------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):  # not the main thread
                return

    def _on_signal(self, signum, frame) -> None:
        del frame
        thread = threading.Thread(
            target=self.drain,
            kwargs={"reason": f"signal {signal.Signals(signum).name}"},
            daemon=True,
            name="sonder-drain",
        )
        thread.start()

    # -- drain -------------------------------------------------------------

    def drain(self, *, reason: str = "shutdown requested") -> bool:
        """Run the full drain sequence; True when it completed cleanly."""
        # Admission and the re-entry decision share this lock, making the
        # Event's otherwise separate check/set operations atomic.
        with self._lock:
            if self._draining.is_set():
                return True
            self._draining.set()
        try:
            self._tracker.transition(ProcessState.DRAINING, reason)
        except Exception:
            # STARTING/MIGRATING processes stop without a DRAINING phase.
            pass
        self._cancellation.cancel()

        clean = True
        deadline = time.monotonic() + self._deadline
        with self._idle:
            while self._active_mutations > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    clean = False
                    break
                self._idle.wait(min(remaining, 0.5))

        if not clean:
            for hook in self._interrupted_hooks:
                try:
                    hook()
                except Exception:
                    pass

        for hook in self._flush_hooks:
            try:
                hook()
            except Exception:
                pass

        try:
            self._tracker.transition(
                ProcessState.STOPPING,
                "drain complete" if clean else "drain deadline expired",
            )
        except Exception:
            pass
        return clean
