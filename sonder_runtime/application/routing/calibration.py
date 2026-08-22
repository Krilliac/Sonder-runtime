"""Measured calibration registry and deterministic model selection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sonder_runtime.domain.routing.calibration_profiles import CalibrationProfile


@dataclass(frozen=True)
class CalibrationSelection:
    profile: CalibrationProfile
    reason: str = "measured-profile"


class CalibrationRegistry:
    """Small in-memory registry; persistence belongs to the composition root."""

    def __init__(self, profiles: Iterable[CalibrationProfile] = ()) -> None:
        self._profiles: dict[tuple[str, str, str, str], CalibrationProfile] = {}
        for profile in profiles:
            self.record(profile)

    def record(self, profile: CalibrationProfile) -> CalibrationProfile:
        """Upsert only when the observation is newer, or equally new but richer."""
        current = self._profiles.get(profile.key)
        if current is None or (profile.measured_at, profile.sample_count, profile.digest()) > (
            current.measured_at, current.sample_count, current.digest()
        ):
            self._profiles[profile.key] = profile
        return self._profiles[profile.key]

    def profiles(self) -> tuple[CalibrationProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: item.key))

    def candidates(
        self,
        *,
        hardware: str,
        models: Iterable[str] | None = None,
        now: datetime | None = None,
        max_age_seconds: float = 7 * 24 * 60 * 60,
        max_resident_gb: float | None = None,
    ) -> tuple[CalibrationProfile, ...]:
        now = now or datetime.now(timezone.utc)
        wanted = {model.strip().lower() for model in models} if models is not None else None
        result = [
            profile for profile in self._profiles.values()
            if profile.hardware.strip().lower() == hardware.strip().lower()
            and (wanted is None or profile.model.strip().lower() in wanted)
            and profile.is_fresh(now, max_age_seconds)
            and (max_resident_gb is None or profile.resident_gb <= max_resident_gb)
        ]
        return tuple(sorted(result, key=lambda item: item.key))

    def select(
        self,
        *,
        hardware: str,
        models: Iterable[str],
        now: datetime | None = None,
        max_age_seconds: float = 7 * 24 * 60 * 60,
        max_resident_gb: float | None = None,
    ) -> CalibrationSelection | None:
        candidates = self.candidates(
            hardware=hardware, models=models, now=now,
            max_age_seconds=max_age_seconds, max_resident_gb=max_resident_gb,
        )
        if not candidates:
            return None
        # Highest observed throughput wins; ties prefer lower p95 latency,
        # lower residency, newer data, then lexical identity for stability.
        chosen = min(candidates, key=lambda item: (
            -item.throughput_tokens_per_second,
            item.latency_ms_p95,
            item.resident_gb,
            -item.measured_at.timestamp(),
            item.key,
        ))
        return CalibrationSelection(chosen)


__all__ = ["CalibrationRegistry", "CalibrationSelection"]
