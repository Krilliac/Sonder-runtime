"""Evidence-gated publication of durable procedural skills.

The WP6 memory and promotion services intentionally stop at candidate/evidence
creation.  This module is the application boundary that binds a candidate
skill to held-out evidence and publishes immutable versions.  The catalog is
append-only and snapshot-able so a host can put it behind a durable store;
the in-memory implementation is useful for deterministic tests and embeds no
filesystem or provider behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping

from ..skill_refresh import SkillRevision


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


class PublicationState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"
    DISABLED = "disabled"


@dataclass(frozen=True)
class HeldOutEvidence:
    """Evidence measured on data not used to form the procedural candidate."""

    suite_id: str
    suite_digest: str
    candidate_digest: str
    baseline_digest: str
    passed: bool
    metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        for name in ("suite_id", "suite_digest", "candidate_digest", "baseline_digest"):
            _required_text(getattr(self, name), name)
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be boolean")
        if not isinstance(self.metrics, Mapping) or not self.metrics:
            raise ValueError("held-out metrics must be non-empty")
        clean: dict[str, float] = {}
        for key, value in self.metrics.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("held-out metric names must be non-empty")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("held-out metrics must be numeric")
            clean[key.strip()] = float(value)
        object.__setattr__(self, "metrics", dict(sorted(clean.items())))

    @property
    def digest(self) -> str:
        payload = {
            "suite_id": self.suite_id,
            "suite_digest": self.suite_digest,
            "candidate_digest": self.candidate_digest,
            "baseline_digest": self.baseline_digest,
            "passed": self.passed,
            "metrics": self.metrics,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SkillPublication:
    """An immutable, versioned skill publication and its provenance."""

    skill_id: str
    version: str
    digest: str
    content: str
    evidence_digest: str
    candidate_memory_id: str
    state: PublicationState = PublicationState.CANDIDATE
    published_at: datetime = _now()

    def __post_init__(self) -> None:
        for name in ("skill_id", "version", "digest", "content", "evidence_digest", "candidate_memory_id"):
            _required_text(getattr(self, name), name)
        if not isinstance(self.state, PublicationState):
            raise ValueError("state must be a PublicationState")


@dataclass(frozen=True)
class CatalogSnapshot:
    """Portable append-only catalog state suitable for durable storage."""

    revisions: tuple[SkillPublication, ...]
    active: tuple[tuple[str, str], ...]
    last_good: tuple[tuple[str, str], ...]
    disabled: tuple[tuple[str, str], ...]
    snapshot_digest: str


class PublicationError(ValueError):
    """Raised when a publication cannot safely change active state."""


class DurableLastGoodCatalog:
    """Append-only version catalog with explicit active/last-good state.

    ``snapshot`` and ``from_snapshot`` are the persistence seam.  A real host
    can atomically store the returned value; this class itself performs no
    I/O and therefore cannot pretend that process memory is durable storage.
    """

    def __init__(self, revisions: tuple[SkillPublication, ...] = ()) -> None:
        self._revisions: list[SkillPublication] = list(revisions)
        self._active: dict[str, str] = {}
        self._last_good: dict[str, str] = {}
        self._disabled: dict[str, str] = {}

    def publish(
        self,
        candidate: SkillPublication,
        revision: SkillRevision,
        evidence: HeldOutEvidence,
    ) -> SkillPublication:
        self._validate(candidate, revision, evidence)
        if candidate.skill_id in self._disabled:
            raise PublicationError("skill is explicitly disabled")
        current = self.current(candidate.skill_id)
        if current is not None:
            self._last_good[candidate.skill_id] = current.version
        active = SkillPublication(
            **{**candidate.__dict__, "state": PublicationState.ACTIVE, "published_at": _now()}
        )
        self._revisions.append(active)
        self._active[active.skill_id] = active.version
        return active

    def current(self, skill_id: str) -> SkillPublication | None:
        version = self._active.get(skill_id)
        return self._find(skill_id, version) if version else None

    def last_good(self, skill_id: str) -> SkillPublication | None:
        version = self._last_good.get(skill_id)
        return self._find(skill_id, version) if version else None

    def rollback(self, skill_id: str) -> SkillPublication:
        if skill_id in self._disabled:
            raise PublicationError("disabled skill must be enabled before rollback")
        target = self.last_good(skill_id)
        if target is None:
            raise PublicationError("no last-good publication is available")
        current = self.current(skill_id)
        if current is not None:
            self._last_good[skill_id] = current.version
        restored = SkillPublication(**{**target.__dict__, "state": PublicationState.ACTIVE, "published_at": _now()})
        self._revisions.append(restored)
        self._active[skill_id] = restored.version
        return restored

    def disable(self, skill_id: str, reason: str) -> None:
        _required_text(reason, "reason")
        if self.current(skill_id) is None:
            raise PublicationError("cannot disable an inactive skill")
        self._disabled[skill_id] = reason.strip()
        current = self.current(skill_id)
        assert current is not None
        self._revisions.append(SkillPublication(**{**current.__dict__, "state": PublicationState.DISABLED, "published_at": _now()}))
        del self._active[skill_id]

    def enable(self, skill_id: str) -> None:
        if skill_id not in self._disabled:
            raise PublicationError("skill is not disabled")
        del self._disabled[skill_id]

    def disabled_reason(self, skill_id: str) -> str | None:
        return self._disabled.get(skill_id)

    def snapshot(self) -> CatalogSnapshot:
        payload = self._payload()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return CatalogSnapshot(tuple(self._revisions), tuple(sorted(self._active.items())), tuple(sorted(self._last_good.items())), tuple(sorted(self._disabled.items())), digest)

    @classmethod
    def from_snapshot(cls, snapshot: CatalogSnapshot) -> "DurableLastGoodCatalog":
        catalog = cls(snapshot.revisions)
        catalog._active = dict(snapshot.active)
        catalog._last_good = dict(snapshot.last_good)
        catalog._disabled = dict(snapshot.disabled)
        if catalog.snapshot().snapshot_digest != snapshot.snapshot_digest:
            raise PublicationError("catalog snapshot integrity check failed")
        return catalog

    def _validate(self, candidate: SkillPublication, revision: SkillRevision, evidence: HeldOutEvidence) -> None:
        if candidate.state is not PublicationState.CANDIDATE:
            raise PublicationError("only candidate publications may be published")
        if revision.name != candidate.skill_id or revision.version != candidate.version or revision.digest != candidate.digest:
            raise PublicationError("skill revision does not match candidate publication")
        if not revision.refresh_allowed(expected_digest=candidate.digest):
            raise PublicationError("skill revision is not trusted and compatible")
        if evidence.candidate_digest != candidate.digest:
            raise PublicationError("held-out evidence is for a different candidate")
        if not evidence.passed:
            raise PublicationError("held-out evidence did not pass")
        if candidate.evidence_digest != evidence.digest:
            raise PublicationError("candidate evidence digest does not match held-out evidence")

    def _find(self, skill_id: str, version: str | None) -> SkillPublication | None:
        if version is None:
            return None
        for publication in reversed(self._revisions):
            if publication.skill_id == skill_id and publication.version == version:
                return publication
        raise PublicationError("catalog points to a missing revision")

    def _payload(self) -> dict[str, Any]:
        return {
            "revisions": [p.__dict__ | {"state": p.state.value, "published_at": p.published_at.isoformat()} for p in self._revisions],
            "active": sorted(self._active.items()),
            "last_good": sorted(self._last_good.items()),
            "disabled": sorted(self._disabled.items()),
        }


__all__ = [
    "CatalogSnapshot", "DurableLastGoodCatalog", "HeldOutEvidence",
    "PublicationError", "PublicationState", "SkillPublication",
]
