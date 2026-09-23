"""Recent, provider-neutral evidence used to admit model routes.

Profiles describe what a model is expected to support.  This module records
what a local backend actually passed recently, so a stale or failed probe
cannot silently become a routing decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .capability_profiles import Capability


class BackendCapability(str, Enum):
    CHAT = "chat"
    STRUCTURED = "structured"
    CANCELLATION = "cancellation"


class EvidenceReason(str, Enum):
    ELIGIBLE = "recent_capability_evidence_passed"
    MISSING = "recent_capability_evidence_missing"
    STALE = "recent_capability_evidence_stale"
    FAILED = "recent_capability_probe_failed"
    SYNTHETIC = "synthetic_capability_evidence"
    FUTURE = "future_capability_evidence"


@dataclass(frozen=True)
class ProbeResult:
    capability: BackendCapability
    passed: bool
    reason_code: str


@dataclass(frozen=True)
class BackendConformanceRecord:
    backend: str
    model: str
    checked_at: float
    results: tuple[ProbeResult, ...]
    probe_version: int = 1
    synthetic: bool = False

    def __post_init__(self) -> None:
        if not self.backend.strip() or not self.model.strip():
            raise ValueError("backend and model are required")
        if self.checked_at < 0 or self.probe_version <= 0:
            raise ValueError("invalid conformance record metadata")
        if len(self.results) > len(BackendCapability):
            raise ValueError("conformance record contains too many probe results")
        if len({result.capability for result in self.results}) != len(self.results):
            raise ValueError("conformance record contains duplicate capabilities")

    @property
    def passed(self) -> frozenset[BackendCapability]:
        return frozenset(result.capability for result in self.results if result.passed)

    @property
    def failed(self) -> frozenset[BackendCapability]:
        return frozenset(result.capability for result in self.results if not result.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "model": self.model,
            "checked_at": self.checked_at,
            "probe_version": self.probe_version,
            "synthetic": self.synthetic,
            "results": [
                {"capability": item.capability.value, "passed": item.passed,
                 "reason_code": item.reason_code}
                for item in self.results
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BackendConformanceRecord":
        raw = value.get("results")
        if not isinstance(raw, list):
            raise ValueError("conformance results must be an array")
        results = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("conformance result must be an object")
            results.append(ProbeResult(
                BackendCapability(item.get("capability", "")),
                item.get("passed") is True,
                str(item.get("reason_code", "")),
            ))
        return cls(
            backend=str(value.get("backend", "")), model=str(value.get("model", "")),
            checked_at=float(value.get("checked_at", -1)), results=tuple(results),
            probe_version=int(value.get("probe_version", 1)),
            synthetic=value.get("synthetic") is True,
        )


def backend_requirements(required: frozenset[Capability]) -> frozenset[BackendCapability]:
    """Translate semantic route requirements into provider smoke requirements."""
    requirements = {BackendCapability.CHAT}
    if Capability.STRUCTURED in required:
        requirements.add(BackendCapability.STRUCTURED)
    return frozenset(requirements)


__all__ = [
    "BackendCapability", "BackendConformanceRecord", "EvidenceReason",
    "ProbeResult", "backend_requirements",
]
