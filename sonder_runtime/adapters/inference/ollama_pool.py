"""Bounded multi-host Ollama inference routing.

The pool is deliberately an inference transport, not a distributed model
runtime. Each worker owns its Ollama process and model files; the coordinator
selects one worker for a request and can fail over only when no response was
received. A completed request is never replayed on another host.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import urllib.error

from sonder_runtime.domain import ollama_policy


_FAILOVER_HTTP_CODES = frozenset({502, 503, 504})
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 30.0


def _is_loopback(origin: str) -> bool:
    host = urlsplit(origin).hostname
    if not host:
        return False
    if host.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_worker_origins(raw: str | None) -> tuple[str, ...]:
    """Parse a bounded comma/semicolon-separated worker origin list."""
    values = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if value:
            values.append(value)
    return tuple(values)


def validate_worker_origin(origin: str, *, allow_remote: bool) -> str:
    """Normalize one worker origin under the Ollama trust policy."""
    normalized = ollama_policy.normalize(origin)
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("worker endpoint must not contain inline credentials")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("worker endpoint must include a host and explicit port")
    if not _is_loopback(normalized):
        if not allow_remote:
            raise ValueError(
                "remote worker endpoints require SONDER_ALLOW_REMOTE_OLLAMA=1"
            )
        if parsed.scheme.casefold() != "https":
            raise ValueError("remote worker endpoints must use https")
    return normalized.rstrip("/")


@dataclass(frozen=True)
class WorkerEndpoint:
    origin: str
    worker_id: str


@dataclass(frozen=True)
class WorkerSnapshot:
    worker_id: str
    origin: str
    healthy: bool
    inflight: int
    consecutive_failures: int
    last_error: str
    cooldown_until: float

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "origin": self.origin,
            "healthy": self.healthy,
            "inflight": self.inflight,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "cooldown_until": self.cooldown_until,
        }


@dataclass
class _WorkerState:
    endpoint: WorkerEndpoint
    inflight: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    cooldown_until: float = 0.0


def _worker_id(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname or "worker"
    port = parsed.port or 443
    return "%s:%s" % (host, port)


class OllamaWorkerPool:
    """Thread-safe least-inflight scheduler for independent Ollama hosts."""

    def __init__(
        self,
        primary_origin: str,
        worker_origins: tuple[str, ...] = (),
        *,
        allow_remote: bool = False,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure threshold must be >= 1")
        if cooldown_seconds < 1:
            raise ValueError("cooldown seconds must be >= 1")
        all_origins = (primary_origin, *worker_origins)
        states = []
        seen = set()
        for raw in all_origins:
            origin = validate_worker_origin(raw, allow_remote=allow_remote)
            if origin in seen:
                continue
            seen.add(origin)
            states.append(_WorkerState(WorkerEndpoint(origin, _worker_id(origin))))
        if not states:
            raise ValueError("at least one Ollama worker is required")
        self._states = states
        self._failure_threshold = int(failure_threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._cursor = 0
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return len(self._states) > 1

    @property
    def has_remote_workers(self) -> bool:
        return any(not _is_loopback(state.endpoint.origin) for state in self._states)

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(state.endpoint.origin for state in self._states)

    def _ordered(self) -> list[_WorkerState]:
        now = time.monotonic()
        with self._lock:
            ready = [
                state for state in self._states
                if state.cooldown_until <= now
            ]
            if not ready:
                ready = [min(self._states, key=lambda item: item.cooldown_until)]
            start = self._cursor % len(ready)
            self._cursor += 1
            rotated = ready[start:] + ready[:start]
            return sorted(rotated, key=lambda item: item.inflight)

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        if isinstance(error, urllib.error.HTTPError):
            return int(error.code or 0) in _FAILOVER_HTTP_CODES
        return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))

    def _start(self, state: _WorkerState) -> None:
        with self._lock:
            state.inflight += 1

    def _finish(
        self,
        state: _WorkerState,
        error: BaseException | None,
        *,
        count_failure: bool = True,
    ) -> None:
        with self._lock:
            state.inflight = max(0, state.inflight - 1)
            if error is None:
                state.consecutive_failures = 0
                state.last_error = ""
                state.cooldown_until = 0.0
                return
            if not count_failure:
                return
            state.consecutive_failures += 1
            state.last_error = "%s: %s" % (type(error).__name__, str(error)[:240])
            if state.consecutive_failures >= self._failure_threshold:
                state.cooldown_until = time.monotonic() + self._cooldown_seconds

    def request(self, sender: Callable[[str], object]):
        """Send once per selected worker, failing over only before a response."""
        last_error = None
        for state in self._ordered():
            self._start(state)
            try:
                result = sender(state.endpoint.origin)
            except Exception as error:
                retryable = self._retryable(error)
                self._finish(state, error, count_failure=retryable)
                if not retryable:
                    raise
                last_error = error
                continue
            self._finish(state, None)
            return result
        if last_error is not None:
            raise last_error
        raise RuntimeError("no Ollama worker is available")

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        now = time.monotonic()
        with self._lock:
            return tuple(
                WorkerSnapshot(
                    worker_id=state.endpoint.worker_id,
                    origin=state.endpoint.origin,
                    healthy=state.cooldown_until <= now,
                    inflight=state.inflight,
                    consecutive_failures=state.consecutive_failures,
                    last_error=state.last_error,
                    cooldown_until=state.cooldown_until,
                )
                for state in self._states
            )

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "worker_count": len(self._states),
            "remote_worker_count": sum(
                1 for state in self._states
                if not _is_loopback(state.endpoint.origin)
            ),
            "workers": [snapshot.to_dict() for snapshot in self.snapshots()],
        }


def from_environment(primary_origin: str, environment=None) -> OllamaWorkerPool:
    """Build the pool from ``SONDER_OLLAMA_WORKERS`` and consent settings."""
    env = os.environ if environment is None else environment
    allow_remote = str(env.get("SONDER_ALLOW_REMOTE_OLLAMA", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    return OllamaWorkerPool(
        primary_origin,
        parse_worker_origins(env.get("SONDER_OLLAMA_WORKERS")),
        allow_remote=allow_remote,
    )


__all__ = [
    "OllamaWorkerPool",
    "WorkerEndpoint",
    "WorkerSnapshot",
    "from_environment",
    "parse_worker_origins",
    "validate_worker_origin",
]
