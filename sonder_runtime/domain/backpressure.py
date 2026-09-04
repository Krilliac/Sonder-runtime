"""Backpressure propagation chain.

Wires fleet pressure signals into admission decisions.  The
``BackpressureChain`` holds a pressure source and makes probabilistic
admission decisions: at low pressure, all requests pass; at critical
pressure, all are shed; in between, a deterministic hash of the
request key decides whether to admit (ensuring consistent behavior
for retries of the same request).

No I/O, no threading -- callers own synchronization and supply the
pressure signal.
"""
from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass
from typing import Callable

from sonder_runtime.domain.fleet_pressure import (
    BAND_CRITICAL,
    BAND_HIGH,
    BAND_LOW,
    BAND_MEDIUM,
    PressureSample,
    admission_factor,
)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    admitted: bool
    factor: float
    band: str
    reason: str


def _request_hash_fraction(key: str) -> float:
    h = hashlib.sha256(key.encode("utf-8")).digest()
    val = struct.unpack(">I", h[:4])[0]
    return val / 0xFFFFFFFF


class BackpressureChain:
    """Probabilistic admission based on fleet pressure.

    ``pressure_source``: callable returning the current pressure band.
    When no source is provided, all requests are admitted.
    """

    def __init__(
        self,
        pressure_source: Callable[[], str] | None = None,
    ) -> None:
        self._source = pressure_source
        self._admitted = 0
        self._shed = 0

    def check(self, request_key: str) -> AdmissionDecision:
        if self._source is None:
            self._admitted += 1
            return AdmissionDecision(
                admitted=True,
                factor=1.0,
                band=BAND_LOW,
                reason="no_pressure_source",
            )

        band = self._source()
        factor = admission_factor(band)

        if factor >= 1.0:
            self._admitted += 1
            return AdmissionDecision(
                admitted=True, factor=factor, band=band, reason="low_pressure"
            )

        if factor <= 0.0:
            self._shed += 1
            return AdmissionDecision(
                admitted=False, factor=factor, band=band, reason="critical_shed"
            )

        request_score = _request_hash_fraction(request_key)
        admitted = request_score < factor

        if admitted:
            self._admitted += 1
            return AdmissionDecision(
                admitted=True,
                factor=factor,
                band=band,
                reason="probabilistic_admit",
            )
        else:
            self._shed += 1
            return AdmissionDecision(
                admitted=False,
                factor=factor,
                band=band,
                reason="probabilistic_shed",
            )

    @property
    def admitted_count(self) -> int:
        return self._admitted

    @property
    def shed_count(self) -> int:
        return self._shed

    def snapshot(self) -> dict[str, int]:
        return {
            "admitted": self._admitted,
            "shed": self._shed,
            "total": self._admitted + self._shed,
        }
