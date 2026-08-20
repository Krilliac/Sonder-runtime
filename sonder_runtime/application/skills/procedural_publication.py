"""Evidence-gated publication of durable procedural skills.

The WP6 memory and promotion services intentionally stop at candidate/evidence
creation.  This module is the application boundary that binds a candidate
skill to held-out evidence and publishes immutable versions.  The catalog is
append-only and snapshot-able so a host can put it behind a durable store;
the in-memory implementation is useful for deterministic tests and embeds no
filesystem or provider behavior.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterator, Mapping, Protocol

from ..skill_refresh import SkillRevision
from ...domain.memory.wp6_typed import TypedMemory
from ...domain.promotion.measured import PromotionDecision
from ...application.memory.memory_policy import link_procedural_promotion


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
    source_interaction_ids: tuple[str, ...] = ()
    rollback_reference: str = ""
    promotion_evidence_digest: str = ""
    published_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        for name in ("skill_id", "version", "digest", "content", "evidence_digest", "candidate_memory_id"):
            _required_text(getattr(self, name), name)
        if not isinstance(self.state, PublicationState):
            raise ValueError("state must be a PublicationState")
        if any(not isinstance(item, str) or not item.strip() for item in self.source_interaction_ids):
            raise ValueError("source interaction identifiers must be non-empty strings")
        if not isinstance(self.rollback_reference, str):
            raise ValueError("rollback_reference must be a string")


class ActiveSkillPort(Protocol):
    """Host-owned active-skill seam used by the publication transaction."""

    def snapshot(self) -> object: ...

    def activate(self, publication: SkillPublication) -> None: ...

    def restore(self, snapshot: object) -> None: ...


class PublicationEventPort(Protocol):
    """Small event seam; event failure is part of the publication transaction."""

    def emit(self, event_code: str, *, summary: str, detail: dict | None = None, **kwargs) -> None: ...


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

    @contextmanager
    def transaction(self) -> Iterator["DurableLastGoodCatalog"]:
        """Apply a publication to an isolated catalog and commit atomically.

        The snapshot is the durable-store port: an adapter may persist the
        returned state in its own transaction, while this implementation gives
        callers deterministic all-or-nothing semantics for tests and hosts
        that keep the catalog in a repository-owned row.
        """
        before = self.snapshot()
        working = type(self).from_snapshot(before)
        try:
            yield working
        except BaseException:
            raise
        else:
            self._restore_from(working.snapshot())

    def restore(self, snapshot: CatalogSnapshot) -> None:
        """Restore a verified snapshot, the explicit rollback persistence seam."""
        restored = type(self).from_snapshot(snapshot)
        self._restore_from(restored.snapshot())

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

    def _restore_from(self, snapshot: CatalogSnapshot) -> None:
        self._revisions = list(snapshot.revisions)
        self._active = dict(snapshot.active)
        self._last_good = dict(snapshot.last_good)
        self._disabled = dict(snapshot.disabled)


class InMemoryActiveSkillPort:
    """Reference active-skill adapter with snapshot/restore semantics."""

    def __init__(self) -> None:
        self._active: dict[str, SkillPublication] = {}

    def snapshot(self) -> object:
        return dict(self._active)

    def activate(self, publication: SkillPublication) -> None:
        if publication.state is not PublicationState.ACTIVE:
            raise PublicationError("only active publications may be activated")
        self._active[publication.skill_id] = publication

    def restore(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            raise PublicationError("active-skill snapshot is invalid")
        self._active = dict(snapshot)

    def current(self, skill_id: str) -> SkillPublication | None:
        return self._active.get(skill_id)


class ProceduralPublicationService:
    """Transactional bridge from memory/promotion evidence to active skills."""

    def __init__(self, catalog: DurableLastGoodCatalog, active: ActiveSkillPort, events: PublicationEventPort | None = None) -> None:
        self.catalog = catalog
        self.active = active
        self.events = events

    def publish(
        self,
        memory: TypedMemory,
        candidate: SkillPublication,
        revision: SkillRevision,
        evidence: HeldOutEvidence,
        decision: PromotionDecision,
        *,
        source_interaction_ids: tuple[str, ...] | list[str],
    ) -> SkillPublication:
        """Commit catalog and active-skill state together or restore both."""
        if not decision.accepted:
            raise PublicationError("promotion decision is not approved")
        if decision.area.value != "skills":
            raise PublicationError("promotion decision is for a different area")
        if decision.candidate not in {candidate.skill_id, f"{candidate.skill_id}@{candidate.version}"}:
            raise PublicationError("promotion decision is for a different candidate")
        if not decision.evidence_digest:
            raise PublicationError("promotion decision is missing evidence provenance")
        if not decision.rollback_reference:
            raise PublicationError("approved promotion is missing rollback provenance")
        link = link_procedural_promotion(
            memory,
            candidate_id=f"{candidate.skill_id}@{candidate.version}",
            source_interaction_ids=source_interaction_ids,
            baseline_digest=evidence.baseline_digest,
            candidate_digest=candidate.digest,
            rollback_reference=decision.rollback_reference or evidence.baseline_digest,
        )
        candidate = SkillPublication(
            **{
                **candidate.__dict__,
                "candidate_memory_id": link.memory_id,
                "source_interaction_ids": link.source_interaction_ids,
                "rollback_reference": link.rollback_reference,
                "promotion_evidence_digest": decision.evidence_digest,
            }
        )
        catalog_before = self.catalog.snapshot()
        active_before = self.active.snapshot()
        try:
            with self.catalog.transaction() as staged:
                published = staged.publish(candidate, revision, evidence)
                self.active.activate(published)
                if self.events is not None:
                    self.events.emit(
                        "procedural_skill_published",
                        summary="procedural skill publication committed",
                        detail={
                            "skill_id": published.skill_id,
                            "version": published.version,
                            "memory_id": published.candidate_memory_id,
                            "evidence_digest": published.evidence_digest,
                            "rollback_reference": published.rollback_reference,
                        },
                    )
        except BaseException as exc:
            self.catalog.restore(catalog_before)
            try:
                self.active.restore(active_before)
            except BaseException as restore_exc:
                raise PublicationError("publication failed and active-skill rollback failed") from restore_exc
            if self.events is not None:
                try:
                    self.events.emit(
                        "procedural_skill_publication_failed",
                        summary="procedural skill publication rolled back",
                        detail={"skill_id": candidate.skill_id, "reason": type(exc).__name__},
                    )
                except BaseException:
                    pass
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError("procedural skill publication rolled back") from exc
        return published

    def rollback(self, skill_id: str) -> SkillPublication:
        """Restore last-good catalog and active skill as one guarded operation."""
        catalog_before = self.catalog.snapshot()
        active_before = self.active.snapshot()
        try:
            with self.catalog.transaction() as staged:
                restored = staged.rollback(skill_id)
                self.active.activate(restored)
        except BaseException as exc:
            self.catalog.restore(catalog_before)
            self.active.restore(active_before)
            if isinstance(exc, PublicationError):
                raise
            raise PublicationError("procedural skill rollback failed") from exc
        return restored


__all__ = [
    "CatalogSnapshot", "DurableLastGoodCatalog", "HeldOutEvidence",
    "ActiveSkillPort", "InMemoryActiveSkillPort", "ProceduralPublicationService",
    "PublicationError", "PublicationEventPort", "PublicationState", "SkillPublication",
]
