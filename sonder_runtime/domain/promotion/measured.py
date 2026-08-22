"""Pure domain model for measured promotion gates.

The domain deliberately does not know how metrics were collected or how a
rollback is executed.  It only carries bounded evidence and the resulting
decision so callers cannot accidentally treat an unmeasured candidate as
promotable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
from typing import Mapping


class PromotionArea(str, Enum):
    SKILLS = "skills"
    ROUTING = "routing"
    MEMORY = "memory"
    MODELS = "models"
    SELFMOD = "selfmod"


class PromotionInputError(ValueError):
    """Raised when evidence or policy is not safe to evaluate."""


def _metrics(values: Mapping[str, float], label: str) -> Mapping[str, float]:
    if not isinstance(values, Mapping) or not values:
        raise PromotionInputError(f"{label} must be a non-empty mapping")
    clean: dict[str, float] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise PromotionInputError(f"{label} contains an invalid metric name")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PromotionInputError(f"{label}.{key} must be a finite number")
        clean[key.strip()] = float(value)
    return MappingProxyType(dict(sorted(clean.items())))


@dataclass(frozen=True)
class MeasuredEvidence:
    """Immutable evidence for one candidate in one promotion area."""

    area: PromotionArea
    candidate: str
    baseline: str
    metrics: Mapping[str, float]
    holdout_passed: bool
    provenance: tuple[str, ...]
    rollback_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.area, PromotionArea):
            raise PromotionInputError("area must be a PromotionArea")
        for name in ("candidate", "baseline"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PromotionInputError(f"{name} must be non-empty")
        if not isinstance(self.rollback_reference, str):
            raise PromotionInputError("rollback_reference must be a string")
        if not isinstance(self.holdout_passed, bool):
            raise PromotionInputError("holdout_passed must be boolean")
        if not isinstance(self.provenance, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.provenance
        ):
            raise PromotionInputError("provenance must be a tuple of non-empty strings")
        object.__setattr__(self, "metrics", _metrics(self.metrics, "metrics"))
        object.__setattr__(self, "candidate", self.candidate.strip())
        object.__setattr__(self, "baseline", self.baseline.strip())
        object.__setattr__(self, "rollback_reference", self.rollback_reference.strip())


@dataclass(frozen=True)
class PromotionPolicy:
    """Thresholds and safety requirements for one promotion area."""

    minimums: Mapping[str, float]
    maximum_regressions: Mapping[str, float] = MappingProxyType({})
    required_provenance: tuple[str, ...] = ()
    require_holdout: bool = True
    require_rollback: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "minimums", _metrics(self.minimums, "minimums"))
        object.__setattr__(self, "maximum_regressions", _metrics(self.maximum_regressions, "maximum_regressions") if self.maximum_regressions else MappingProxyType({}))
        if not isinstance(self.require_holdout, bool) or not isinstance(self.require_rollback, bool):
            raise PromotionInputError("policy safety flags must be boolean")
        if any(not isinstance(item, str) or not item.strip() for item in self.required_provenance):
            raise PromotionInputError("required_provenance must contain non-empty strings")


@dataclass(frozen=True)
class PromotionDecision:
    """Auditable result; acceptance always carries a rollback reference."""

    accepted: bool
    reason: str
    area: PromotionArea
    candidate: str
    failed_gates: tuple[str, ...] = ()
    rollback_reference: str | None = None
    evidence_digest: str = ""


__all__ = [
    "MeasuredEvidence", "PromotionArea", "PromotionDecision",
    "PromotionInputError", "PromotionPolicy",
]
