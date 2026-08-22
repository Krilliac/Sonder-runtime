"""Thread-safe runtime admission gate used by graceful drain."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    accepting: bool
    stop_reason: str | None
    accepted: int
    rejected: int


class AdmissionClosed(RuntimeError):
    """Raised when work arrives after admission has been stopped."""


class RuntimeAdmissionGate:
    """Own the admission state shared by HTTP, jobs, and workflow callers."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._accepting = True
        self._reason: str | None = None
        self._accepted = 0
        self._rejected = 0

    def stop_admission(self, reason: str) -> bool:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("admission stop reason is required")
        with self._lock:
            if not self._accepting:
                return False
            self._accepting = False
            self._reason = reason
            return True

    def admit(self) -> None:
        with self._lock:
            if not self._accepting:
                self._rejected += 1
                raise AdmissionClosed(self._reason or "admission stopped")
            self._accepted += 1

    def snapshot(self) -> AdmissionSnapshot:
        with self._lock:
            return AdmissionSnapshot(self._accepting, self._reason, self._accepted, self._rejected)


__all__ = ["AdmissionClosed", "AdmissionSnapshot", "RuntimeAdmissionGate"]
