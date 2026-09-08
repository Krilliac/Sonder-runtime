"""Canonical graceful-shutdown coordination boundary."""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

import signal
import threading
import time

from sonder_runtime.platform.service_state import ProcessState, ServiceStateTracker
from sonder_runtime.platform.process import CancellationToken


class ShutdownCoordinator:
    def __init__(self, tracker: ServiceStateTracker, *, drain_deadline_seconds: float = 25.0) -> None:
        self._tracker = tracker
        self._deadline = drain_deadline_seconds
        self._lock = threading.Lock()
        self._active_mutations = 0
        self._idle = threading.Condition(self._lock)
        self._draining = threading.Event()
        self._cancellation = CancellationToken()
        self._flush_hooks: list = []
        self._interrupted_hooks: list = []
        self._signal_drain = self.drain

    def begin_mutation(self) -> bool:
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

    def add_flush_hook(self, hook) -> None:
        self._flush_hooks.append(hook)

    def add_interrupted_hook(self, hook) -> None:
        self._interrupted_hooks.append(hook)

    def install_signal_handlers(self, drain_callback=None) -> None:
        if drain_callback is not None:
            self._signal_drain = drain_callback
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                return

    def _on_signal(self, signum, frame) -> None:
        del frame
        owned_runtime_thread(
            target=self._signal_drain,
            kwargs={"reason": f"signal {signal.Signals(signum).name}"},
            daemon=True,
            name="sonder-drain",
        ).start()

    def drain(self, *, reason: str = "shutdown requested") -> bool:
        with self._lock:
            if self._draining.is_set():
                return True
            self._draining.set()
        try:
            self._tracker.transition(ProcessState.DRAINING, reason)
        except Exception:
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
            self._tracker.transition(ProcessState.STOPPING, "drain complete" if clean else "drain deadline expired")
        except Exception:
            pass
        return clean
