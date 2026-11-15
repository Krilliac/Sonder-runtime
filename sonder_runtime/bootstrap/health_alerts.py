"""Compose the health alert service from configuration and run one pass."""
from __future__ import annotations

from collections.abc import Mapping

from sonder_runtime.adapters.health_alerts.notifiers import (
    FanoutNotifier,
    NtfyNotifier,
    ToastNotifier,
)
from sonder_runtime.adapters.health_alerts.schtasks import SchtasksReader
from sonder_runtime.adapters.health_alerts.state_store import (
    JsonAlertStateStore,
    SystemHealthClock,
)
from sonder_runtime.application.health_alerts import (
    AlertPolicy,
    AlertRun,
    HealthAlertService,
)
from sonder_runtime.application.ports.health_alerts import Notifier
from sonder_runtime.domain.health_alerts import parse_watched_task
from sonder_runtime.platform import paths
from sonder_runtime.platform.config import SonderConfig


def build_notifier(config: SonderConfig) -> Notifier:
    settings = config.health_alerts
    channels: list[Notifier] = []
    if settings.toast:
        channels.append(ToastNotifier(timeout_seconds=settings.timeout_seconds))
    if settings.ntfy and config.secrets.health_alert_ntfy_url:
        channels.append(NtfyNotifier(
            config.secrets.health_alert_ntfy_url, timeout_seconds=settings.timeout_seconds))
    return FanoutNotifier(channels)


def build_policy(config: SonderConfig) -> AlertPolicy:
    settings = config.health_alerts
    return AlertPolicy(
        rate_limit_seconds=float(settings.rate_limit_seconds),
        known_off=settings.known_off,
        watched_tasks=tuple(
            parse_watched_task(entry, default_window_hours=settings.default_window_hours)
            for entry in settings.watched_jobs
        ),
        include_warn=settings.include_warn,
    )


def build_service(config: SonderConfig) -> HealthAlertService:
    settings = config.health_alerts
    state_file = settings.state_file or paths.state_path("health_alerts.json")
    return HealthAlertService(
        build_notifier(config), JsonAlertStateStore(state_file),
        SystemHealthClock(), build_policy(config),
    )


def run_health_alerts(
    config: SonderConfig, doctor_report: Mapping[str, object] | None,
    *, service: HealthAlertService | None = None,
) -> AlertRun:
    """Alert on failed doctor checks and on watched jobs that missed their window."""
    active = service if service is not None else build_service(config)
    reader = SchtasksReader(timeout_seconds=config.health_alerts.timeout_seconds)
    return active.check(doctor_report, reader)


__all__ = ["build_notifier", "build_policy", "build_service", "run_health_alerts"]
