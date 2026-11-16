"""Process-wide per-username login backoff, wired to config and the clock.

The decision logic lives in ``sonder_runtime.domain.login_throttle``; this
adapter owns the lock, the environment-driven policy and the monotonic clock.
Limits: ``SONDER_LOGIN_USERNAME_THRESHOLD`` (failures before backoff),
``SONDER_LOGIN_USERNAME_DECAY_SECONDS`` (seconds for one failure to decay) and
``SONDER_LOGIN_USERNAME_MAX_ENTRIES`` (memory bound).  Tests override through
:func:`configure`.
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

from sonder_runtime.domain.login_throttle import (
    LoginDecision,
    LoginThrottlePolicy,
    UsernameBackoff,
)

THRESHOLD_ENV = "SONDER_LOGIN_USERNAME_THRESHOLD"
DECAY_SECONDS_ENV = "SONDER_LOGIN_USERNAME_DECAY_SECONDS"
MAX_ENTRIES_ENV = "SONDER_LOGIN_USERNAME_MAX_ENTRIES"


class LoginThrottled(PermissionError):
    """The username is inside its backoff window; carries the wait."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many failed login attempts")
        self.retry_after_seconds = retry_after_seconds


_N = TypeVar("_N", int, float)


def _env_number(name: str, default: _N, cast: Callable[[Any], _N]) -> _N:
    try:
        value = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def policy_from_env() -> LoginThrottlePolicy:
    defaults = LoginThrottlePolicy()
    return LoginThrottlePolicy(
        threshold=_env_number(THRESHOLD_ENV, defaults.threshold, int),
        decay_seconds=_env_number(DECAY_SECONDS_ENV, defaults.decay_seconds, float),
        max_entries=_env_number(MAX_ENTRIES_ENV, defaults.max_entries, int),
    )


_lock = threading.Lock()
_state: UsernameBackoff | None = None


def configure(policy: LoginThrottlePolicy | None = None) -> None:
    """Replace the live state (fresh and empty); ``None`` re-reads the env."""
    global _state
    with _lock:
        _state = UsernameBackoff(policy or policy_from_env())


def _backoff() -> UsernameBackoff:
    global _state
    if _state is None:
        _state = UsernameBackoff(policy_from_env())
    return _state


def _is_loopback(client: str | None) -> bool:
    try:
        return bool(client) and ipaddress.ip_address(cast(str, client)).is_loopback
    except ValueError:
        return False


def check(username: str, client: str | None = None) -> LoginDecision:
    """``client`` is the resolved client address (trusted-proxy aware).

    A loopback or previously successful (familiar) client bypasses the
    per-username window; it remains subject to the per-IP limiter.
    """
    with _lock:
        return _backoff().check(
            username, time.monotonic(),
            client=client or "", trusted=_is_loopback(client),
        )


def record_failure(username: str) -> None:
    with _lock:
        _backoff().record_failure(username, time.monotonic())


def record_success(username: str, client: str | None = None) -> None:
    with _lock:
        _backoff().record_success(username, time.monotonic(), client or "")
