"""Pure reference-aware retention policy for content-addressed artifact caches.

The policy operates on immutable cache metadata and bounded snapshots supplied by
an adapter.  It never opens a store, resolves a path, contacts a peer, or
performs deletion.  A caller may apply the returned plan only after validating
that its snapshot revisions still match the plan's revisions.

Every cache identity is the tuple ``(artifact_id, digest, version)``.  The
artifact ID prevents a digest collision from changing logical ownership, while
the digest and version prevent a stale generation from being treated as the
current object.  Incomplete reference or tombstone snapshots always defer
cleanup because an omitted owner or tombstone could change the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import re


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_REVISION = (1 << 63) - 1
_MAX_REASON_BYTES = 512


class ArtifactGcAction(StrEnum):
    """Safe action for one cache entry."""

    RETAIN = "retain"
    DELETE = "delete"
    DEFER = "defer"


class ArtifactGcReason(StrEnum):
    """Stable explanation for a retention or cleanup decision."""

    ELIGIBLE = "eligible"
    RETENTION_WINDOW = "retention_window"
    LIVE_JOB_REFERENCE = "live_job_reference"
    LIVE_DEPLOYMENT_REFERENCE = "live_deployment_reference"
    REFERENCE_IDENTITY_MISMATCH = "reference_identity_mismatch"
    REFERENCE_LEASE_EXPIRED = "reference_lease_expired"
    REFERENCE_STATE_UNKNOWN = "reference_state_unknown"
    REFERENCE_CONFLICT = "reference_conflict"
    REFERENCE_SCAN_INCOMPLETE = "reference_scan_incomplete"
    RETENTION_TOMBSTONE = "retention_tombstone"
    DELETION_TOMBSTONE = "deletion_tombstone"
    TOMBSTONE_IDENTITY_MISMATCH = "tombstone_identity_mismatch"
    TOMBSTONE_CONFLICT = "tombstone_conflict"
    TOMBSTONE_SCAN_INCOMPLETE = "tombstone_scan_incomplete"
    CANDIDATE_IDENTITY_CONFLICT = "candidate_identity_conflict"


class ArtifactReferenceKind(StrEnum):
    """Durable owner types allowed to pin an artifact."""

    JOB = "job"
    DEPLOYMENT = "deployment"


class ArtifactReferenceState(StrEnum):
    """Lifecycle state reported by the owner registry."""

    LIVE = "live"
    RELEASED = "released"
    UNKNOWN = "unknown"


class ArtifactTombstoneKind(StrEnum):
    """Durable cache lifecycle markers."""

    DELETION = "deletion"
    RETENTION = "retention"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _version(value: object, field: str = "version") -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded cache generation")
    return value


def _digest(value: object, field: str = "digest") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object, field: str = "revision") -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise ValueError(f"{field} must be a positive bounded integer")
    return value


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > _MAX_REASON_BYTES:
        raise ValueError("reason must be bounded non-empty text")
    return value


def _enum(value: object, enum_type: type[StrEnum], field: str):
    if not isinstance(value, enum_type):
        raise ValueError(f"{field} must be a {enum_type.__name__}")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactCacheEntry:
    """Immutable metadata for one content-addressed cache entry."""

    artifact_id: str
    digest: str
    version: str
    size_bytes: int
    last_accessed_at: datetime
    retain_until: datetime | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        _identity(self.artifact_id, "artifact_id")
        _digest(self.digest)
        _version(self.version)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        _aware(self.last_accessed_at, "last_accessed_at")
        if self.retain_until is not None:
            _aware(self.retain_until, "retain_until")
        _revision(self.revision)

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the logical ID, content digest, and generation tuple."""
        return self.artifact_id, self.digest, self.version


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """One owner assertion that may pin an exact artifact generation."""

    owner_id: str
    owner_kind: ArtifactReferenceKind
    state: ArtifactReferenceState
    artifact_id: str
    digest: str
    version: str
    revision: int = 1
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _identity(self.owner_id, "owner_id")
        _enum(self.owner_kind, ArtifactReferenceKind, "owner_kind")
        _enum(self.state, ArtifactReferenceState, "state")
        _identity(self.artifact_id, "artifact_id")
        _digest(self.digest)
        _version(self.version)
        _revision(self.revision)
        if self.lease_expires_at is not None:
            _aware(self.lease_expires_at, "lease_expires_at")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.artifact_id, self.digest, self.version

    @property
    def owner_key(self) -> tuple[str, str]:
        return self.owner_id, self.artifact_id

    def matches(self, entry: ArtifactCacheEntry) -> bool:
        return isinstance(entry, ArtifactCacheEntry) and self.key == entry.key

    def is_live(self, *, now: datetime) -> bool:
        """Return true only for a live, unexpired owner lease."""
        _aware(now, "now")
        return self.state is ArtifactReferenceState.LIVE and (
            self.lease_expires_at is None or now < self.lease_expires_at
        )

    def lease_expired(self, *, now: datetime) -> bool:
        _aware(now, "now")
        return self.state is ArtifactReferenceState.LIVE and (
            self.lease_expires_at is not None and now >= self.lease_expires_at
        )


@dataclass(frozen=True, slots=True)
class ArtifactTombstone:
    """Digest/version-bound durable hold or deletion marker."""

    artifact_id: str
    digest: str
    version: str
    kind: ArtifactTombstoneKind
    revision: int
    created_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _identity(self.artifact_id, "artifact_id")
        _digest(self.digest)
        _version(self.version)
        _enum(self.kind, ArtifactTombstoneKind, "kind")
        _revision(self.revision)
        _aware(self.created_at, "created_at")
        _reason(self.reason)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.artifact_id, self.digest, self.version


@dataclass(frozen=True, slots=True)
class ArtifactCachePage:
    """Bounded adapter page of cache entries."""

    items: tuple[ArtifactCacheEntry, ...]
    complete: bool = True
    next_cursor: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ArtifactCacheEntry) for item in self.items
        ):
            raise ValueError("items must be a tuple of ArtifactCacheEntry values")
        if type(self.complete) is not bool:
            raise ValueError("complete must be boolean")
        if self.next_cursor is not None:
            _identity(self.next_cursor, "next_cursor")
        _revision(self.revision)


@dataclass(frozen=True, slots=True)
class ArtifactReferencePage:
    """Bounded adapter page of owner-reference assertions."""

    items: tuple[ArtifactReference, ...]
    complete: bool = True
    next_cursor: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ArtifactReference) for item in self.items
        ):
            raise ValueError("items must be a tuple of ArtifactReference values")
        if type(self.complete) is not bool:
            raise ValueError("complete must be boolean")
        if self.next_cursor is not None:
            _identity(self.next_cursor, "next_cursor")
        _revision(self.revision)


@dataclass(frozen=True, slots=True)
class ArtifactTombstonePage:
    """Bounded adapter page of retention/deletion markers."""

    items: tuple[ArtifactTombstone, ...]
    complete: bool = True
    next_cursor: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ArtifactTombstone) for item in self.items
        ):
            raise ValueError("items must be a tuple of ArtifactTombstone values")
        if type(self.complete) is not bool:
            raise ValueError("complete must be boolean")
        if self.next_cursor is not None:
            _identity(self.next_cursor, "next_cursor")
        _revision(self.revision)


@dataclass(frozen=True, slots=True)
class ArtifactGcPolicy:
    """Bounded scan and age limits for one garbage-collection planning pass."""

    max_candidates: int = 256
    max_references: int = 1024
    max_tombstones: int = 1024
    minimum_idle_seconds: int = 3600

    def __post_init__(self) -> None:
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 4096:
            raise ValueError("max_candidates must be within 1..4096")
        if type(self.max_references) is not int or not 1 <= self.max_references <= 8192:
            raise ValueError("max_references must be within 1..8192")
        if type(self.max_tombstones) is not int or not 1 <= self.max_tombstones <= 8192:
            raise ValueError("max_tombstones must be within 1..8192")
        if type(self.minimum_idle_seconds) is not int or not 0 <= self.minimum_idle_seconds <= 365 * 86400:
            raise ValueError("minimum_idle_seconds must be within 0..365 days")


@dataclass(frozen=True, slots=True)
class ArtifactGcDecision:
    """One deterministic, digest/version-bound cache lifecycle decision."""

    artifact_id: str
    digest: str
    version: str
    action: ArtifactGcAction
    reason: ArtifactGcReason
    tombstone: ArtifactTombstone | None = None

    def __post_init__(self) -> None:
        _identity(self.artifact_id, "artifact_id")
        _digest(self.digest)
        _version(self.version)
        _enum(self.action, ArtifactGcAction, "action")
        _enum(self.reason, ArtifactGcReason, "reason")
        if self.tombstone is not None and not isinstance(self.tombstone, ArtifactTombstone):
            raise ValueError("tombstone must be an ArtifactTombstone or None")
        key = (self.artifact_id, self.digest, self.version)
        if self.tombstone is not None and self.tombstone.key != key:
            raise ValueError("tombstone identity must match decision identity")
        if self.action is ArtifactGcAction.DELETE:
            if self.tombstone is None or self.tombstone.kind is not ArtifactTombstoneKind.DELETION:
                raise ValueError("delete decisions require a matching deletion tombstone")
            if self.reason not in (ArtifactGcReason.ELIGIBLE, ArtifactGcReason.DELETION_TOMBSTONE):
                raise ValueError("delete decisions require an eligibility or deletion-tombstone reason")
        elif self.action is ArtifactGcAction.RETAIN:
            if self.reason is ArtifactGcReason.RETENTION_TOMBSTONE and (
                self.tombstone is None or self.tombstone.kind is not ArtifactTombstoneKind.RETENTION
            ):
                raise ValueError("retention tombstone decisions require a retention tombstone")
            if self.tombstone is not None and self.tombstone.kind is not ArtifactTombstoneKind.RETENTION:
                raise ValueError("retain decisions may carry only a retention tombstone")
            if self.tombstone is not None and self.reason is not ArtifactGcReason.RETENTION_TOMBSTONE:
                raise ValueError("retain decisions with a tombstone require a retention-tombstone reason")
        elif self.tombstone is not None:
            raise ValueError("defer decisions cannot carry a tombstone")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.artifact_id, self.digest, self.version


@dataclass(frozen=True, slots=True)
class ArtifactGcPlan:
    """A bounded plan plus snapshot evidence for an adapter to apply later."""

    decisions: tuple[ArtifactGcDecision, ...]
    candidate_revision: int
    reference_revision: int
    tombstone_revision: int
    complete: bool
    next_cursor: str | None
    scanned_candidates: int
    scanned_references: int
    scanned_tombstones: int

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, tuple) or any(
            not isinstance(item, ArtifactGcDecision) for item in self.decisions
        ):
            raise ValueError("decisions must be a tuple of ArtifactGcDecision values")
        _revision(self.candidate_revision, "candidate_revision")
        _revision(self.reference_revision, "reference_revision")
        _revision(self.tombstone_revision, "tombstone_revision")
        if type(self.complete) is not bool:
            raise ValueError("complete must be boolean")
        if self.next_cursor is not None:
            _identity(self.next_cursor, "next_cursor")
        for value, field in (
            (self.scanned_candidates, "scanned_candidates"),
            (self.scanned_references, "scanned_references"),
            (self.scanned_tombstones, "scanned_tombstones"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")

    @property
    def deferred(self) -> tuple[ArtifactGcDecision, ...]:
        return tuple(item for item in self.decisions if item.action is ArtifactGcAction.DEFER)


_DEFAULT_POLICY = ArtifactGcPolicy()


def _default_reference_page() -> ArtifactReferencePage:
    return ArtifactReferencePage(())


def _default_tombstone_page() -> ArtifactTombstonePage:
    return ArtifactTombstonePage(())


def _reference_conflicts(references: tuple[ArtifactReference, ...]) -> set[tuple[str, str]]:
    """Find owner slots carrying contradictory assertions in one snapshot."""
    grouped: dict[tuple[str, str], set[tuple[str, str, ArtifactReferenceState, datetime | None]]] = {}
    for reference in references:
        grouped.setdefault(reference.owner_key, set()).add(
            (reference.digest, reference.version, reference.state, reference.lease_expires_at)
        )
    return {key for key, values in grouped.items() if len(values) > 1}


def _tombstone_for(
    entry: ArtifactCacheEntry,
    tombstones: tuple[ArtifactTombstone, ...],
) -> tuple[ArtifactTombstone | None, ArtifactGcReason | None]:
    same_id = tuple(item for item in tombstones if item.artifact_id == entry.artifact_id)
    exact = tuple(item for item in same_id if item.key == entry.key)
    if any(item.key != entry.key for item in same_id):
        return None, ArtifactGcReason.TOMBSTONE_IDENTITY_MISMATCH
    if not exact:
        return None, None
    kinds = {item.kind for item in exact}
    if len(kinds) > 1:
        return None, ArtifactGcReason.TOMBSTONE_CONFLICT
    latest_revision = max(item.revision for item in exact)
    latest = tuple(item for item in exact if item.revision == latest_revision)
    if len({item.reason for item in latest}) > 1:
        return None, ArtifactGcReason.TOMBSTONE_CONFLICT
    marker = latest[0]
    if marker.kind is ArtifactTombstoneKind.RETENTION:
        return marker, ArtifactGcReason.RETENTION_TOMBSTONE
    return marker, ArtifactGcReason.DELETION_TOMBSTONE


def _decision(
    entry: ArtifactCacheEntry,
    action: ArtifactGcAction,
    reason: ArtifactGcReason,
    tombstone: ArtifactTombstone | None = None,
) -> ArtifactGcDecision:
    return ArtifactGcDecision(
        entry.artifact_id,
        entry.digest,
        entry.version,
        action,
        reason,
        tombstone,
    )


def plan_artifact_gc(
    candidates: ArtifactCachePage,
    references: ArtifactReferencePage | None = None,
    tombstones: ArtifactTombstonePage | None = None,
    *,
    now: datetime | None = None,
    policy: ArtifactGcPolicy | None = None,
) -> ArtifactGcPlan:
    """Build a bounded, fail-closed retention/deletion plan.

    The reference and tombstone pages are authoritative snapshots only when
    their ``complete`` flags are true and they fit the policy bounds.  An
    incomplete owner or tombstone scan defers every candidate in the pass.
    Candidate pages may be continued with ``next_cursor``; entries already in
    a bounded page can still be decided when the owner/tombstone snapshots are
    complete.  The returned revisions let an adapter reject a plan if any
    source changed before applying it.
    """
    if not isinstance(candidates, ArtifactCachePage):
        raise ValueError("candidates must be an ArtifactCachePage")
    references = _default_reference_page() if references is None else references
    tombstones = _default_tombstone_page() if tombstones is None else tombstones
    if not isinstance(references, ArtifactReferencePage):
        raise ValueError("references must be an ArtifactReferencePage")
    if not isinstance(tombstones, ArtifactTombstonePage):
        raise ValueError("tombstones must be an ArtifactTombstonePage")
    policy = _DEFAULT_POLICY if policy is None else policy
    if not isinstance(policy, ArtifactGcPolicy):
        raise ValueError("policy must be an ArtifactGcPolicy")
    current = datetime.now(timezone.utc) if now is None else _aware(now, "now")

    candidate_overflow = len(candidates.items) > policy.max_candidates
    reference_overflow = len(references.items) > policy.max_references
    tombstone_overflow = len(tombstones.items) > policy.max_tombstones
    selected_candidates = tuple(candidates.items[: policy.max_candidates])
    selected_references = tuple(references.items[: policy.max_references])
    selected_tombstones = tuple(tombstones.items[: policy.max_tombstones])
    selected_candidates = tuple(sorted(selected_candidates, key=lambda item: item.key))

    candidate_groups: dict[str, list[ArtifactCacheEntry]] = {}
    for item in selected_candidates:
        candidate_groups.setdefault(item.artifact_id, []).append(item)
    candidate_conflicts = {
        artifact_id
        for artifact_id, items in candidate_groups.items()
        if len({item.key for item in items}) > 1
    }

    reference_conflicts = _reference_conflicts(selected_references)
    reference_scan_incomplete = not references.complete or reference_overflow
    tombstone_scan_incomplete = not tombstones.complete or tombstone_overflow

    decisions: list[ArtifactGcDecision] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for item in selected_candidates:
        if item.key in seen_keys:
            continue
        seen_keys.add(item.key)
        if item.artifact_id in candidate_conflicts:
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.CANDIDATE_IDENTITY_CONFLICT))
            continue
        if reference_scan_incomplete:
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.REFERENCE_SCAN_INCOMPLETE))
            continue
        if tombstone_scan_incomplete:
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.TOMBSTONE_SCAN_INCOMPLETE))
            continue

        same_id_references = tuple(
            reference for reference in selected_references
            if reference.artifact_id == item.artifact_id
        )
        if any(reference.owner_key in reference_conflicts for reference in same_id_references):
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.REFERENCE_CONFLICT))
            continue
        if any(
            reference.key != item.key
            and reference.state in (ArtifactReferenceState.LIVE, ArtifactReferenceState.UNKNOWN)
            for reference in same_id_references
        ):
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.REFERENCE_IDENTITY_MISMATCH))
            continue
        matching_references = tuple(
            reference for reference in same_id_references if reference.key == item.key
        )
        if any(reference.state is ArtifactReferenceState.UNKNOWN for reference in matching_references):
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.REFERENCE_STATE_UNKNOWN))
            continue
        if any(reference.lease_expired(now=current) for reference in matching_references):
            decisions.append(_decision(item, ArtifactGcAction.DEFER, ArtifactGcReason.REFERENCE_LEASE_EXPIRED))
            continue
        live_reference = next(
            (reference for reference in matching_references if reference.is_live(now=current)),
            None,
        )
        if live_reference is not None:
            reason = (
                ArtifactGcReason.LIVE_JOB_REFERENCE
                if live_reference.owner_kind is ArtifactReferenceKind.JOB
                else ArtifactGcReason.LIVE_DEPLOYMENT_REFERENCE
            )
            decisions.append(_decision(item, ArtifactGcAction.RETAIN, reason))
            continue

        marker, marker_reason = _tombstone_for(item, selected_tombstones)
        if marker_reason is ArtifactGcReason.TOMBSTONE_IDENTITY_MISMATCH:
            decisions.append(_decision(item, ArtifactGcAction.DEFER, marker_reason))
            continue
        if marker_reason is ArtifactGcReason.TOMBSTONE_CONFLICT:
            decisions.append(_decision(item, ArtifactGcAction.DEFER, marker_reason))
            continue
        if marker_reason is ArtifactGcReason.RETENTION_TOMBSTONE:
            decisions.append(_decision(item, ArtifactGcAction.RETAIN, marker_reason, marker))
            continue
        if marker_reason is ArtifactGcReason.DELETION_TOMBSTONE:
            decisions.append(_decision(item, ArtifactGcAction.DELETE, marker_reason, marker))
            continue

        idle_deadline = item.last_accessed_at + timedelta(seconds=policy.minimum_idle_seconds)
        if (item.retain_until is not None and current < item.retain_until) or current < idle_deadline:
            decisions.append(_decision(item, ArtifactGcAction.RETAIN, ArtifactGcReason.RETENTION_WINDOW))
            continue
        generated = ArtifactTombstone(
            item.artifact_id,
            item.digest,
            item.version,
            ArtifactTombstoneKind.DELETION,
            item.revision,
            current,
            "garbage collection eligibility decision",
        )
        decisions.append(_decision(item, ArtifactGcAction.DELETE, ArtifactGcReason.ELIGIBLE, generated))

    complete = (
        candidates.complete
        and not candidate_overflow
        and references.complete
        and not reference_overflow
        and tombstones.complete
        and not tombstone_overflow
    )
    next_cursor = None
    if not candidates.complete or candidate_overflow:
        next_cursor = candidates.next_cursor
        if next_cursor is None and selected_candidates:
            next_cursor = selected_candidates[-1].artifact_id
    return ArtifactGcPlan(
        tuple(decisions),
        candidates.revision,
        references.revision,
        tombstones.revision,
        complete,
        next_cursor,
        len(selected_candidates),
        len(selected_references),
        len(selected_tombstones),
    )


__all__ = [
    "ArtifactCacheEntry",
    "ArtifactCachePage",
    "ArtifactGcAction",
    "ArtifactGcDecision",
    "ArtifactGcPlan",
    "ArtifactGcPolicy",
    "ArtifactGcReason",
    "ArtifactReference",
    "ArtifactReferenceKind",
    "ArtifactReferencePage",
    "ArtifactReferenceState",
    "ArtifactTombstone",
    "ArtifactTombstoneKind",
    "ArtifactTombstonePage",
    "plan_artifact_gc",
]
