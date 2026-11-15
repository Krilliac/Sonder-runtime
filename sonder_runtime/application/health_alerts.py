"""Health alert service: turn failed checks and missed jobs into rate-limited notifications."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from sonder_runtime.application.ports.health_alerts import (
    AlertStateStore,
    DeliveryResult,
    HealthClock,
    Notifier,
    ScheduledTaskReader,
    TaskUnavailable,
)
from sonder_runtime.domain.health_alerts import (
    Alert,
    AlertKind,
    WatchedTask,
    alerts_from_doctor_report,
    is_known_off,
    prune_sent,
    rate_limit_allows,
    task_alert,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertPolicy:
    rate_limit_seconds: float
    known_off: tuple[str, ...] = ()
    watched_tasks: tuple[WatchedTask, ...] = ()
    include_warn: bool = False


@dataclass(frozen=True)
class AlertRun:
    """What one pass did, for the caller and for tests."""

    sent: tuple[Alert, ...] = ()
    suppressed: tuple[Alert, ...] = ()
    known_off: tuple[Alert, ...] = ()
    undelivered: tuple[Alert, ...] = ()
    deliveries: tuple[DeliveryResult, ...] = field(default=())

    @property
    def problems(self) -> int:
        return len(self.sent) + len(self.suppressed) + len(self.undelivered)


class HealthAlertService:
    def __init__(
        self, notifier: Notifier, store: AlertStateStore,
        clock: HealthClock, policy: AlertPolicy,
    ) -> None:
        self._notifier = notifier
        self._store = store
        self._clock = clock
        self._policy = policy

    def check(
        self, doctor_report: Mapping[str, object] | None,
        tasks: ScheduledTaskReader | None,
    ) -> AlertRun:
        """Collect problems from the doctor report and the watched jobs, then announce."""
        alerts: list[Alert] = []
        if doctor_report is not None:
            alerts.extend(alerts_from_doctor_report(
                doctor_report, include_warn=self._policy.include_warn))
        if tasks is not None:
            alerts.extend(self._task_alerts(tasks))
        return self.announce(alerts)

    def _task_alerts(self, reader: ScheduledTaskReader) -> list[Alert]:
        now = self._clock.now_local()
        out: list[Alert] = []
        for watched in self._policy.watched_tasks:
            if is_known_off(watched.name, self._policy.known_off):
                continue
            try:
                state = reader.read(watched.name)
            except TaskUnavailable as exc:
                out.append(Alert(
                    AlertKind.TASK_UNREADABLE, watched.name,
                    "Scheduled job %s cannot be read" % watched.name, str(exc)[:200]))
                continue
            alert = task_alert(state, watched.window_hours, now)
            if alert is not None:
                out.append(alert)
        return out

    def announce(self, alerts: list[Alert]) -> AlertRun:
        policy = self._policy
        now = self._clock.epoch()
        sent_state = prune_sent(self._store.load(), now, policy.rate_limit_seconds)
        sent: list[Alert] = []
        suppressed: list[Alert] = []
        off: list[Alert] = []
        failed: list[Alert] = []
        deliveries: list[DeliveryResult] = []
        seen: set[str] = set()
        for alert in alerts:
            if alert.key in seen:
                continue
            seen.add(alert.key)
            if is_known_off(alert.subject, policy.known_off):
                off.append(alert)
                continue
            if not rate_limit_allows(sent_state.get(alert.key), now, policy.rate_limit_seconds):
                suppressed.append(alert)
                continue
            results = self._notifier.send(alert)
            deliveries.extend(results)
            if any(r.delivered for r in results):
                sent_state[alert.key] = now
                sent.append(alert)
            else:
                logger.error("health alert %s was not delivered on any channel", alert.key)
                failed.append(alert)
        if sent:
            self._store.save(sent_state)
        return AlertRun(tuple(sent), tuple(suppressed), tuple(off), tuple(failed), tuple(deliveries))


__all__ = ["AlertPolicy", "AlertRun", "HealthAlertService"]
