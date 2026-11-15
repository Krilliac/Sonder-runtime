"""Operator settings for health alerts (failed self-checks, missed scheduled jobs).

The ntfy push URL is secret-like and lives in ``Secrets.health_alert_ntfy_url``
(``SONDER_HEALTH_ALERT_NTFY_URL``), never in this section or in TOML.
"""
import re
from dataclasses import dataclass

_HTTP_URL = re.compile(r"https?://[^\s/?#@]+(?:[/?#]\S*)?", re.IGNORECASE)


def watched_job_entry_valid(entry, default_window_hours):
    """Platform-side mirror of ``domain.health_alerts.parse_watched_task`` (a drift test pins them)."""
    text = entry.strip()
    name, sep, hours = text.rpartition("=") if "=" in text else (text, "", "")
    window = default_window_hours
    if sep:
        try:
            window = float(hours)
        except ValueError:
            return False
    return bool(name.strip() if sep else text) and 0 < window <= 24 * 366


@dataclass(frozen=True)
class HealthAlertsConfig:
    # Windows toast through Windows PowerShell; ignored on other platforms.
    toast: bool = True
    # ntfy-style HTTP push. Off unless enabled here AND a URL is set in secrets.
    ntfy: bool = False
    # One alert per problem per period.
    rate_limit_seconds: int = 6 * 3600
    # "Name" or "Name=hours": scheduled tasks whose last run is checked.
    watched_jobs: tuple[str, ...] = ()
    # Window used by watched jobs without an explicit "=hours".
    default_window_hours: float = 26.0
    # Jobs and doctor checks that are off on purpose; they never alert.
    known_off: tuple[str, ...] = ()
    # Also alert on doctor warnings, not only failures.
    include_warn: bool = False
    # Rate-limit state file; empty means <state home>/health_alerts.json.
    state_file: str = ""
    # Seconds allowed to each delivery attempt and to each schtasks query.
    timeout_seconds: float = 10.0


def health_alerts_errors(config):
    c = config.health_alerts
    errors = []
    for key in ("toast", "ntfy", "include_warn"):
        if type(getattr(c, key)) is not bool:
            errors.append(f"[health_alerts].{key} must be boolean")
    for key in ("watched_jobs", "known_off"):
        values = getattr(c, key)
        if (
            type(values) is not tuple
            or len(values) > 64
            or any(type(v) is not str or not v.strip() or len(v) > 200 for v in values)
        ):
            errors.append(f"[health_alerts].{key} must be up to 64 non-empty strings")
        elif key == "watched_jobs":
            window = c.default_window_hours
            default = window if type(window) is float and 0.0 < window <= 24.0 * 366 else 26.0
            for entry in values:
                if not watched_job_entry_valid(entry, default):
                    errors.append(
                        '[health_alerts].watched_jobs entries must be "Name" or "Name=hours" '
                        "with hours in (0, 8784]"
                    )
                    break
    if type(c.rate_limit_seconds) is not int or not 60 <= c.rate_limit_seconds <= 30 * 86400:
        errors.append("[health_alerts].rate_limit_seconds must be an integer in [60, 2592000]")
    for key, low, high in (("default_window_hours", 0.0, 24.0 * 366), ("timeout_seconds", 0.0, 120.0)):
        value = getattr(c, key)
        if type(value) is not float or not low < value <= high:
            errors.append(f"[health_alerts].{key} must be a number in ({low:g}, {high:g}]")
    if type(c.state_file) is not str:
        errors.append("[health_alerts].state_file must be a string")
    url = config.secrets.health_alert_ntfy_url
    if c.ntfy and not url:
        errors.append("[health_alerts] ntfy enabled but SONDER_HEALTH_ALERT_NTFY_URL is not set")
    if url:
        if not _HTTP_URL.fullmatch(url):
            # The URL itself is never echoed.
            errors.append("SONDER_HEALTH_ALERT_NTFY_URL must be an http(s) URL")
    return errors
