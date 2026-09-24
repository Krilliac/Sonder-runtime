"""Opt-in, process-shared physical model request velocity admission.

Ollama and OpenAI-compatible sends share one durable bucket per Sonder state
home. A host must configure both burst and requests per minute; no limit is
guessed for an unconfigured deployment. SQLite serializes each physical send
admission, including requests from another process and after a restart.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sonder_runtime.adapters.filesystem.atomic_json import file_lock
from sonder_runtime.adapters.persistence.owned_sqlite import transaction
from sonder_runtime.domain.token_bucket import AcquireResult
from sonder_runtime.platform.paths import state_path

_DATABASE_NAME = "model-request-admission.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_request_rate (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    tokens REAL NOT NULL,
    last_refill REAL NOT NULL,
    burst INTEGER NOT NULL,
    requests_per_minute INTEGER NOT NULL
)
"""


class ModelRequestAdmissionError(RuntimeError):
    """The configured shared admission state is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class ModelRequestRateConfig:
    burst: int
    requests_per_minute: int

    def __post_init__(self):
        if (type(self.burst) is not int or not 1 <= self.burst <= 256 or
                type(self.requests_per_minute) is not int or
                not 1 <= self.requests_per_minute <= 1200):
            raise ValueError("model request rate exceeds host configuration ceiling")


class HostModelRequestAdmission:
    """One bounded, transactional bucket shared by processes with the same home."""

    def __init__(
        self,
        config: ModelRequestRateConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
        db_path: str | Path | None = None,
    ):
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        # Typed startup can bind its state home after this module is imported.
        # Pin the path at first admission, after the host has finalized it.
        self._path = Path(db_path) if config and db_path is not None else None
        self._initialized = False

    @classmethod
    def from_environ(
        cls,
        env: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
        db_path: str | Path | None = None,
    ) -> HostModelRequestAdmission:
        burst = env.get("SONDER_MODEL_REQUEST_BURST")
        rate = env.get("SONDER_MODEL_REQUESTS_PER_MINUTE")
        if burst is None and rate is None:
            return cls(clock=clock)
        if burst is None or rate is None:
            raise ValueError("both host model request rate settings must be configured")
        if not burst.isdecimal() or not rate.isdecimal():
            raise ValueError("model request rate settings must be positive integers")
        bounded_burst, bounded_rate = int(burst), int(rate)
        if not (1 <= bounded_burst <= 256 and 1 <= bounded_rate <= 1200):
            raise ValueError("model request rate exceeds host configuration ceiling")
        if db_path is None:
            # Host startup may bind a typed home without changing os.environ.
            # Explicit snapshots still honor their own supplied home.
            home = (env.get("SONDER_STATE_HOME") or env.get("SONDER_HOME")) if env is not os.environ else ""
            db_path = Path(home).expanduser() / _DATABASE_NAME if home else None
        return cls(ModelRequestRateConfig(bounded_burst, bounded_rate),
                   clock=clock, db_path=db_path)

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def _claim_store_creation(self) -> bool:
        """Remember first use under the already-held, persistent OS lock."""
        lock_path = self._path.with_name(self._path.name + ".lock")
        with lock_path.open("r+b") as marker:
            # The shared file-lock helper may append NUL padding while two
            # cold openers initialize its lock byte. Only byte 1 is our marker;
            # no helper overwrites that byte after it becomes non-NUL. Avoid
            # byte 0 entirely: Windows holds a mandatory lock on that byte.
            marker.seek(1)
            state = marker.read(1)
            if state == b"I":
                return False
            if state not in (b"", b"\0"):
                raise ValueError("invalid admission initialization marker")
            marker.seek(1)
            marker.write(b"I")
            marker.flush()
            os.fsync(marker.fileno())
        return True

    def try_acquire(self) -> AcquireResult | None:
        if self.config is None:
            return None
        try:
            now = float(self._clock())
            if not math.isfinite(now) or now < 0:
                raise ValueError("invalid admission clock")
            with self._lock:
                if self._path is None:
                    self._path = Path(state_path(_DATABASE_NAME))
                if not self._initialized:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self._path.with_name(self._path.name + ".lock")
                if self._path.is_symlink() or lock_path.is_symlink():
                    raise ValueError("admission state cannot use symlinks")
                if self._path.exists() and not lock_path.exists():
                    raise ValueError("admission initialization marker is missing")
                # SQLite creates its file before BEGIN IMMEDIATE. Serialize that
                # first creation as well, so a second cold process cannot mistake
                # the first process's empty file for a damaged existing store.
                with file_lock(self._path, timeout=1.0):
                    existing_database = self._path.exists()
                    first_creation = self._claim_store_creation()
                    if not existing_database and (not first_creation or self._initialized):
                        raise ValueError("admission database is missing")
                    with transaction(str(self._path), timeout=1.0) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        if not self._initialized:
                            connection.execute(_SCHEMA)
                        row = connection.execute(
                            "SELECT tokens,last_refill,burst,requests_per_minute "
                            "FROM model_request_rate WHERE id=1"
                        ).fetchone()
                        if row is None:
                            # First boot creates the table and initial token in a
                            # single transaction. An existing empty DB (or a row
                            # removed since this process started) is damaged
                            # state, not a free new burst.
                            if existing_database or self._initialized:
                                raise ValueError("admission row is missing")
                            tokens, effective_now = float(self.config.burst), now
                        else:
                            tokens, previous_time, previous_burst, previous_rate = row
                            if (type(tokens) not in {float, int} or not math.isfinite(tokens)
                                    or type(previous_time) not in {float, int}
                                    or not math.isfinite(previous_time) or previous_time < 0
                                    or type(previous_burst) is not int or not 1 <= previous_burst <= 256
                                    or type(previous_rate) is not int or not 1 <= previous_rate <= 1200
                                    or not 0 <= tokens <= previous_burst):
                                raise ValueError("invalid admission state")
                            effective_now = max(now, previous_time)
                            effective_burst = min(previous_burst, self.config.burst)
                            effective_rate = min(previous_rate, self.config.requests_per_minute)
                            if (previous_burst, previous_rate) != (effective_burst, effective_rate):
                                # Only tighten policy while mixed-version owners
                                # use one state home. A higher-rate owner cannot
                                # silently relax a limit another owner installed.
                                tokens = min(tokens, effective_burst)
                            else:
                                tokens = min(effective_burst, tokens +
                                             (effective_now - previous_time) *
                                             effective_rate / 60)
                        allowed = tokens >= 1
                        remaining = max(0.0, tokens - 1) if allowed else tokens
                        if row is None:
                            effective_burst = self.config.burst
                            effective_rate = self.config.requests_per_minute
                        connection.execute(
                            "INSERT INTO model_request_rate(id,tokens,last_refill,burst,requests_per_minute) "
                            "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                            "tokens=excluded.tokens,last_refill=excluded.last_refill,"
                            "burst=excluded.burst,requests_per_minute=excluded.requests_per_minute",
                            (remaining, effective_now, effective_burst, effective_rate),
                        )
                    self._initialized = True
            wait = 0.0 if allowed else max(
                0.0001, round((1 - remaining) * 60 / effective_rate, 4),
            )
            return AcquireResult(allowed, remaining, wait)
        except (sqlite3.Error, OSError, ValueError, OverflowError, RuntimeError) as error:
            # An unreadable/corrupt state must never silently reset capacity.
            raise ModelRequestAdmissionError("model request admission unavailable") from error


# Initialized once by the host module, including standalone OpenAI graph
# composition. Individual gateway instances all use this same authority.
_HOST_MODEL_REQUEST_ADMISSION = HostModelRequestAdmission.from_environ(os.environ)


def host_model_request_admission() -> HostModelRequestAdmission:
    return _HOST_MODEL_REQUEST_ADMISSION
