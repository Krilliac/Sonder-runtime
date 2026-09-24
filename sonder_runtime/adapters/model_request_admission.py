"""Opt-in, host-wide physical model request velocity admission.

The Ollama and OpenAI-compatible model send paths share one startup snapshot
of SONDER_MODEL_REQUEST_BURST and SONDER_MODEL_REQUESTS_PER_MINUTE. Both must be present,
or both absent (disabled). A host chooses values from measured workload;
there is no guessed default throttling legitimate model jobs. This process
bucket is deliberately not presented as a cross-process or restart budget.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sonder_runtime.domain.token_bucket import AcquireResult, TokenBucket


@dataclass(frozen=True, slots=True)
class ModelRequestRateConfig:
    burst: int
    requests_per_minute: int


class HostModelRequestAdmission:
    """A synchronized physical-request token bucket owned by the host."""

    def __init__(
        self,
        config: ModelRequestRateConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._bucket = (
            TokenBucket(config.burst, config.requests_per_minute / 60)
            if config is not None else None
        )

    @classmethod
    def from_environ(
        cls,
        env: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.monotonic,
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
        return cls(ModelRequestRateConfig(bounded_burst, bounded_rate), clock=clock)

    @property
    def enabled(self) -> bool:
        return self._bucket is not None

    def try_acquire(self) -> AcquireResult | None:
        if self._bucket is None:
            return None
        with self._lock:
            return self._bucket.try_acquire(now=self._clock())


# Initialized once by the host module, including standalone OpenAI graph
# composition. Individual gateway instances all use this same authority.
_HOST_MODEL_REQUEST_ADMISSION = HostModelRequestAdmission.from_environ(os.environ)


def host_model_request_admission() -> HostModelRequestAdmission:
    return _HOST_MODEL_REQUEST_ADMISSION
