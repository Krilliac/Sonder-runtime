"""Pure policy for health alerts: what is a problem, and when to say so again.

No I/O. A failed doctor check or a scheduled task that missed its window
becomes an :class:`Alert` with a stable ``key`` (one key per problem), and
:func:`rate_limit_allows` decides whether that key may be announced again.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sonder_runtime.domain.doctor_status import STATUS_FAIL, STATUS_WARN

__all__ = [
    "Alert",
    "AlertKind",
    "DateOrder",
    "MAX_FUTURE_SKEW",
    "ScheduledTaskState",
    "TaskParseError",
    "WatchedTask",
    "alerts_from_doctor_report",
    "is_known_off",
    "parse_schtasks_csv",
    "parse_watched_task",
    "prune_sent",
    "rate_limit_allows",
    "task_alert",
]

# schtasks "Last Result" values that are not failures.
_RESULT_OK = 0
_RESULT_RUNNING = 267009  # SCHED_S_TASK_RUNNING
_RESULT_NOT_YET_RUN = 267011  # SCHED_S_TASK_HAS_NOT_RUN

_NEVER_RUN_TEXT = ("n/a", "never", "")
# schtasks prints this placeholder for a task that has never run.
_NEVER_RUN_YEAR = 1999

# A last run further ahead of the local clock than this cannot be real.
MAX_FUTURE_SKEW = timedelta(minutes=5)


class DateOrder(str, Enum):
    """Order of day, month and year in the scheduler's printed dates."""

    MDY = "mdy"
    DMY = "dmy"
    YMD = "ymd"
    UNKNOWN = "unknown"


# Year-first text is unambiguous whatever the user's short-date order is.
_YEAR_FIRST_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)
_MDY_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S")
_DMY_FORMATS = ("%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S")
_FORMATS_BY_ORDER = {
    DateOrder.MDY: _MDY_FORMATS + _YEAR_FIRST_FORMATS,
    DateOrder.DMY: _DMY_FORMATS + _YEAR_FIRST_FORMATS,
    DateOrder.YMD: _YEAR_FIRST_FORMATS,
    DateOrder.UNKNOWN: _MDY_FORMATS + _DMY_FORMATS + _YEAR_FIRST_FORMATS,
}

MAX_TITLE_CHARS = 120
MAX_DETAIL_CHARS = 400


class AlertKind(str, Enum):
    DOCTOR_CHECK = "doctor_check"
    TASK_MISSED = "task_missed"
    TASK_FAILED = "task_failed"
    TASK_UNREADABLE = "task_unreadable"


class TaskParseError(ValueError):
    """The scheduler output could not be understood (typed, never a bare string match)."""


@dataclass(frozen=True)
class Alert:
    """One problem worth telling the operator about."""

    kind: AlertKind
    subject: str
    title: str
    detail: str

    @property
    def key(self) -> str:
        return "%s:%s" % (self.kind.value, self.subject.casefold())


@dataclass(frozen=True)
class WatchedTask:
    name: str
    window_hours: float


@dataclass(frozen=True)
class ScheduledTaskState:
    name: str
    scheduler_state: str
    last_run: datetime | None
    last_result: int | None


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def is_known_off(subject: str, known_off: Iterable[str]) -> bool:
    folded = subject.strip().casefold()
    return any(folded == item.strip().casefold() for item in known_off)


def alerts_from_doctor_report(
    report: Mapping[str, object], *, include_warn: bool = False,
) -> tuple[Alert, ...]:
    """One alert per failed check (and per warning when ``include_warn``)."""
    checks = report.get("checks")
    if not isinstance(checks, (list, tuple)):
        return ()
    wanted = {STATUS_FAIL, STATUS_WARN} if include_warn else {STATUS_FAIL}
    out: list[Alert] = []
    for entry in checks:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status", "")).strip().lower()
        if status not in wanted:
            continue
        name = str(entry.get("name", "")).strip() or "unnamed"
        out.append(Alert(
            kind=AlertKind.DOCTOR_CHECK,
            subject=name,
            title=_clip("Sonder health check %s: %s" % (status, name), MAX_TITLE_CHARS),
            detail=_clip(str(entry.get("detail", "")), MAX_DETAIL_CHARS),
        ))
    return tuple(out)


def parse_watched_task(entry: str, *, default_window_hours: float) -> WatchedTask:
    """``"Name"`` or ``"Name=hours"``; raises ValueError on a bad entry."""
    text = entry.strip()
    name, sep, hours = text.rpartition("=") if "=" in text else (text, "", "")
    if not sep:
        name, window = text, default_window_hours
    else:
        try:
            window = float(hours)
        except ValueError as exc:
            raise ValueError("watched task window is not a number: %r" % entry) from exc
    name = name.strip()
    if not name:
        raise ValueError("watched task has no name: %r" % entry)
    if not 0 < window <= 24 * 366:
        raise ValueError("watched task window must be within (0, 8784] hours: %r" % entry)
    return WatchedTask(name=name, window_hours=float(window))


def _parse_time(text: str, date_order: DateOrder) -> datetime | None:
    """Parse a last-run time using only the formats of ``date_order``.

    Text that two readings turn into different moments (``12/01/2027`` as 1 Dec
    or 12 Jan) is refused rather than guessed.
    """
    value = text.strip()
    if value.casefold() in _NEVER_RUN_TEXT:
        return None
    readings: set[datetime] = set()
    for fmt in _FORMATS_BY_ORDER[date_order]:
        try:
            readings.add(datetime.strptime(value, fmt))
        except ValueError:
            continue
    if not readings:
        raise TaskParseError("unrecognised last-run time format")
    if len(readings) > 1:
        raise TaskParseError("last-run date is ambiguous between day-first and month-first")
    (parsed,) = readings
    return None if parsed.year <= _NEVER_RUN_YEAR else parsed


def parse_schtasks_csv(
    text: str, name: str, *, date_order: DateOrder = DateOrder.UNKNOWN,
) -> ScheduledTaskState:
    """Parse ``schtasks /query /tn <name> /fo csv /v`` output for one task.

    ``date_order`` is the user's short-date order (read by the adapter);
    ``UNKNOWN`` accepts only dates that mean the same under every reading.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    rows = [row for row in rows if row.get("TaskName") != "TaskName"]
    if not rows:
        raise TaskParseError("scheduler returned no rows")
    wanted = name.strip().lstrip("\\").casefold()
    row = next(
        (r for r in rows if (r.get("TaskName") or "").lstrip("\\").casefold() == wanted),
        rows[0] if len(rows) == 1 else None,
    )
    if row is None:
        raise TaskParseError("task not present in scheduler output")
    for column in ("Last Run Time", "Last Result", "Scheduled Task State"):
        if column not in row:
            raise TaskParseError("scheduler output lacks column %r" % column)
    raw_result = (row["Last Result"] or "").strip()
    try:
        result: int | None = int(raw_result)
    except ValueError:
        result = None
    return ScheduledTaskState(
        name=name,
        scheduler_state=(row["Scheduled Task State"] or "").strip(),
        last_run=_parse_time(row["Last Run Time"] or "", date_order),
        last_result=result,
    )


def task_alert(
    state: ScheduledTaskState, window_hours: float, now: datetime,
) -> Alert | None:
    """An alert when the task missed its window or its last run failed, else None.

    ``now`` is naive local time, the same clock ``schtasks`` prints in. Raises
    :class:`TaskParseError` for a last run in the future: it must not read as healthy.
    """
    window = timedelta(hours=window_hours)
    result = state.last_result
    if result == _RESULT_NOT_YET_RUN or state.last_run is None:
        return Alert(
            AlertKind.TASK_MISSED, state.name,
            _clip("Scheduled job %s has never run" % state.name, MAX_TITLE_CHARS),
            "No last run time; expected within %g h." % window_hours,
        )
    age = now - state.last_run
    if age < -MAX_FUTURE_SKEW:
        raise TaskParseError("last-run time is in the future")
    if age > window:
        return Alert(
            AlertKind.TASK_MISSED, state.name,
            _clip("Scheduled job %s missed its window" % state.name, MAX_TITLE_CHARS),
            "Last run %s, %.1f h ago; expected within %g h." % (
                state.last_run.strftime("%Y-%m-%d %H:%M"),
                age.total_seconds() / 3600.0, window_hours),
        )
    if result not in (None, _RESULT_OK, _RESULT_RUNNING):
        return Alert(
            AlertKind.TASK_FAILED, state.name,
            _clip("Scheduled job %s failed" % state.name, MAX_TITLE_CHARS),
            "Last run %s ended with result %d." % (
                state.last_run.strftime("%Y-%m-%d %H:%M"), result),
        )
    return None


def rate_limit_allows(last_sent: float | None, now: float, period_seconds: float) -> bool:
    """True when this problem has not been announced within the period.

    A clock that moved backwards (``now < last_sent``) allows the alert: a
    stale future timestamp must not silence a problem indefinitely.
    """
    if last_sent is None or now < last_sent:
        return True
    return now - last_sent >= period_seconds


def prune_sent(
    sent: Mapping[str, float], now: float, period_seconds: float,
) -> dict[str, float]:
    """Drop entries old enough that they can no longer suppress anything."""
    horizon = max(period_seconds, 1.0) * 10
    return {k: v for k, v in sent.items() if 0 <= now - v < horizon or v > now}
