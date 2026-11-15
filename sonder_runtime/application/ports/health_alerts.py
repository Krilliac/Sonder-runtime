"""Ports for health alerts: where alerts go, where rate-limit state lives, what the scheduler says."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sonder_runtime.domain.health_alerts import Alert, ScheduledTaskState


# Channel name of the always-on log fallback; it never counts as an operator being told.
LOG_CHANNEL = "log"


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one channel. ``error`` is a short class-level reason and never
    carries a URL, header or other secret-like value."""

    channel: str
    delivered: bool
    error: str = ""


class Notifier(Protocol):
    def send(self, alert: Alert) -> tuple[DeliveryResult, ...]: ...


class AlertStateStore(Protocol):
    """Last-sent epoch seconds per problem key; survives process restarts."""

    def load(self) -> dict[str, float]: ...

    def save(self, sent: dict[str, float]) -> None: ...


class ScheduledTaskReader(Protocol):
    def read(self, name: str) -> ScheduledTaskState:
        """Raise ``TaskUnavailable`` when the task or the scheduler cannot be read."""
        ...


class TaskUnavailable(RuntimeError):
    """The scheduler could not report this task (missing, access denied, unparseable)."""


class HealthClock(Protocol):
    def now_local(self) -> datetime:
        """Naive local time, the clock the scheduler prints in."""
        ...

    def epoch(self) -> float: ...


__all__ = [
    "AlertStateStore", "DeliveryResult", "HealthClock", "LOG_CHANNEL", "Notifier",
    "ScheduledTaskReader", "TaskUnavailable",
]
