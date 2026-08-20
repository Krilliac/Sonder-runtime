"""Measured model residency and performance profiles.

Profiles are observations, not promises.  They are keyed by the exact model
artifact, quantization, and hardware identity so a result measured for one
machine cannot silently become a routing fact for another.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CalibrationProfile:
    """One measured model/hardware observation."""

    model: str
    hardware: str
    quantization: str
    resident_gb: float
    throughput_tokens_per_second: float
    latency_ms_p95: float
    measured_at: datetime
    sample_count: int = 1
    source: str = "local-measurement"

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.hardware.strip() or not self.quantization.strip():
            raise ValueError("model, hardware, and quantization are required")
        if self.resident_gb <= 0 or self.throughput_tokens_per_second <= 0 or self.latency_ms_p95 <= 0:
            raise ValueError("residency, throughput, and latency must be positive")
        if self.sample_count < 1:
            raise ValueError("sample_count must be positive")
        object.__setattr__(self, "measured_at", _utc(self.measured_at))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.model.strip().lower(), self.hardware.strip().lower(), self.quantization.strip().lower(), self.source.strip().lower())

    def age_seconds(self, now: datetime) -> float:
        return max(0.0, (_utc(now) - self.measured_at).total_seconds())

    def is_fresh(self, now: datetime, max_age_seconds: float) -> bool:
        return max_age_seconds >= 0 and self.age_seconds(now) <= max_age_seconds

    def digest(self) -> str:
        payload = {
            "key": self.key,
            "resident_gb": self.resident_gb,
            "throughput_tokens_per_second": self.throughput_tokens_per_second,
            "latency_ms_p95": self.latency_ms_p95,
            "measured_at": self.measured_at.isoformat(),
            "sample_count": self.sample_count,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = ["CalibrationProfile"]
