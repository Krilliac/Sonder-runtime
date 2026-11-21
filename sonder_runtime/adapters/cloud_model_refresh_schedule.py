"""Daily cloud model refresh, off the request path, for long-lived serve and MCP processes.

Sonder has no general periodic scheduler.  ``serve`` and ``mcp`` start their
post-startup maintenance as one-shot owned daemon threads; this module adds
one more such thread.  It waits ``FIRST_DELAY_SECONDS`` so startup and the
first request are untouched, then checks once an hour whether a refresh is
due: never refreshed, or the persisted ``last_success_at`` in the refresh
state file is at least ``MIN_INTERVAL_SECONDS`` old, and no attempt in this
process failed within ``RETRY_SECONDS``.  A due run is ``run_refresh(apply=True,
trigger="schedule")``; its run lock plus the persisted timestamp make a serve
and an MCP process on one state home refresh once a day, not twice.

Disabled by ``SONDER_CLOUD_MODEL_REFRESH=0`` or ``auto_refresh: false`` in the
preferences file (and by default in tests).  The thread only logs, never
prints: MCP's stdout is its protocol channel.  Every failure is swallowed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import sonder_runtime.adapters.cloud_model_refresh as refresh
import sonder_runtime.domain.cloud_model_refresh as policy
from sonder_runtime.platform import runtime_threads

logger = logging.getLogger(__name__)

ENV_SWITCH = "SONDER_CLOUD_MODEL_REFRESH"
THREAD_NAME = "sonder-cloud-model-refresh"
MIN_INTERVAL_SECONDS = refresh.REFRESH_INTERVAL_SECONDS
FIRST_DELAY_SECONDS = 120
RETRY_SECONDS = 6 * 3600
# The one recurring check; None makes the schedule startup-only.
RECHECK_SECONDS: float | None = 3600
_OFF = frozenset({"0", "false", "no", "off"})

Runner = Callable[..., dict[str, Any]]


def enabled(env: Mapping[str, str], prefs: policy.Preferences | None) -> bool:
    """The env switch is on (default) and the preferences do not say ``auto_refresh: false``."""
    if str(env.get(ENV_SWITCH, "1")).strip().lower() in _OFF:
        return False
    return prefs is None or prefs.auto_refresh


def due(state: Mapping[str, Any] | None, now: float, last_attempt: float | None) -> bool:
    """Due when never refreshed or a day old, unless this process failed in the last 6 h."""
    if last_attempt is not None and now - last_attempt < RETRY_SECONDS:
        return False
    stamp = policy.parse_timestamp((state or {}).get("last_success_at"))
    return stamp is None or now - stamp >= MIN_INTERVAL_SECONDS


def _check_once(
    *,
    origin: str,
    home: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    last_failure: float | None,
    clock: Callable[[], float],
    runner: Runner,
) -> float | None:
    """Run one due check; returns the new last-failure time."""
    environment = os.environ if env is None else env
    paths = refresh.RefreshPaths.resolve(home)
    prefs, _error = refresh.load_preferences(paths.preferences)
    if not enabled(environment, prefs):
        return last_failure
    state, _state_error = refresh.load_state(paths.state)
    now = clock()
    if not due(state, now, last_failure):
        return last_failure
    report = runner(origin=origin, apply=True, trigger="schedule", home=home, env=env)
    outcome = report.get("outcome")
    logger.info("scheduled cloud model refresh: %s %s", outcome, report.get("reason") or "")
    if outcome == "ok":
        return None
    return last_failure if outcome == "busy" else now


def _loop(
    *,
    origin: str,
    home: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    stop: threading.Event,
    clock: Callable[[], float] = time.time,
    runner: Runner | None = None,
) -> None:
    """Thread body: delayed start, then a due check every ``RECHECK_SECONDS``."""
    run = runner if runner is not None else refresh.run_refresh
    last_failure: float | None = None
    if stop.wait(FIRST_DELAY_SECONDS):
        return
    while True:
        try:
            last_failure = _check_once(
                origin=origin, home=home, env=env, last_failure=last_failure,
                clock=clock, runner=run,
            )
        except Exception:  # noqa: BLE001 - maintenance never takes its host down
            logger.warning("scheduled cloud model refresh failed", exc_info=True)
            last_failure = clock()
        if RECHECK_SECONDS is None or stop.wait(RECHECK_SECONDS):
            return


def start_scheduled_refresh(
    *,
    origin: str,
    thread_factory: Callable[..., Any] = runtime_threads.Thread,
    home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stop: threading.Event | None = None,
) -> bool:
    """Start the daily refresh thread unless disabled; returns whether it started. Never raises."""
    try:
        environment = os.environ if env is None else env
        if str(environment.get(ENV_SWITCH, "1")).strip().lower() in _OFF:
            return False
        prefs, _error = refresh.load_preferences(refresh.RefreshPaths.resolve(home).preferences)
        if not enabled(environment, prefs):
            return False
        thread = thread_factory(
            target=_loop,
            kwargs={"origin": origin, "home": home, "env": env,
                    "stop": stop if stop is not None else threading.Event()},
            daemon=True,
            name=THREAD_NAME,
        )
        thread.start()
        return True
    except Exception:  # noqa: BLE001 - a failed hook must not stop serve or MCP startup
        logger.warning("cloud model refresh schedule did not start", exc_info=True)
        return False


__all__ = [
    "ENV_SWITCH",
    "FIRST_DELAY_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "RECHECK_SECONDS",
    "RETRY_SECONDS",
    "THREAD_NAME",
    "due",
    "enabled",
    "start_scheduled_refresh",
]
