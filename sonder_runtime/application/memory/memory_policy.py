"""Application policy for the remaining typed-memory integration gap.

This module is deliberately side-effect free.  ``memory_store`` remains the
SQLite adapter and ``wp6_typed`` remains the domain model for evidence-backed
memories.  The policy here supplies the decisions those ports need: class
specific admission and recall rules, temporal truth, vector-space identity,
and an auditable link from a procedural memory to a promotion candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ...domain.memory.wp6_typed import MemoryLabel, TypedMemory


class MemoryClass(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    PROJECT = "project"
    FAILURE = "failure"
    ARTIFACT = "artifact"


class PrivacyClass(str, Enum):
    PUBLIC = "public"
    PROJECT = "project"
    PRIVATE = "private"
    SECRET = "secret"


class RetrievalDisposition(str, Enum):
    ALLOW = "allow"
    EXCLUDE = "exclude"


class PromotionDisposition(str, Enum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    ROLLBACK_REQUIRED = "rollback_required"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


@dataclass(frozen=True)
class MemoryClassPolicy:
    """The bounded lifecycle contract for one memory class."""

    memory_class: MemoryClass
    write_min_confidence: float
    write_requires_provenance: bool
    write_requires_explicit_or_evidence: bool
    retrieval_enabled: bool
    retrieval_requires_scope: bool
    default_max_age: timedelta | None
    promotion_enabled: bool
    export_allowed: bool
    deletion_allowed: bool
    private_by_default: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.write_min_confidence <= 1.0:
            raise ValueError("write_min_confidence must be between 0 and 1")
        if self.default_max_age is not None and self.default_max_age < timedelta(0):
            raise ValueError("default_max_age must not be negative")


# Policy is data, so callers can inspect the complete per-class matrix without
# reaching into a store.  Short-lived working memory is never promoted or
# exported; secrets are never admitted into this memory surface.
MEMORY_CLASS_POLICIES: Mapping[MemoryClass, MemoryClassPolicy] = {
    MemoryClass.WORKING: MemoryClassPolicy(
        MemoryClass.WORKING, 0.0, False, False, True, False,
        timedelta(hours=24), False, False, True,
    ),
    MemoryClass.EPISODIC: MemoryClassPolicy(
        MemoryClass.EPISODIC, 0.50, True, True, True, True,
        timedelta(days=90), False, True, True,
    ),
    MemoryClass.SEMANTIC: MemoryClassPolicy(
        MemoryClass.SEMANTIC, 0.70, True, True, True, True,
        timedelta(days=365), False, True, True,
    ),
    MemoryClass.PROCEDURAL: MemoryClassPolicy(
        MemoryClass.PROCEDURAL, 0.80, True, True, True, True,
        timedelta(days=180), True, True, True,
    ),
    MemoryClass.PREFERENCE: MemoryClassPolicy(
        MemoryClass.PREFERENCE, 0.80, True, True, True, True,
        timedelta(days=365), False, False, True,
        private_by_default=True,
    ),
    MemoryClass.PROJECT: MemoryClassPolicy(
        MemoryClass.PROJECT, 0.60, True, True, True, True,
        None, False, True, True,
    ),
    MemoryClass.FAILURE: MemoryClassPolicy(
        MemoryClass.FAILURE, 0.60, True, True, True, True,
        timedelta(days=365), False, False, True,
    ),
    MemoryClass.ARTIFACT: MemoryClassPolicy(
        MemoryClass.ARTIFACT, 0.70, True, True, True, True,
        None, False, True, True,
    ),
}


def policy_for(memory_class: MemoryClass | str) -> MemoryClassPolicy:
    """Return the immutable policy for a class."""
    try:
        kind = memory_class if isinstance(memory_class, MemoryClass) else MemoryClass(memory_class)
        return MEMORY_CLASS_POLICIES[kind]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown memory class: {memory_class!r}") from exc


def _resolve_class_policy(
    kind: MemoryClass, policy: MemoryClassPolicy | None,
) -> MemoryClassPolicy:
    """Return the policy an evaluator must enforce for ``kind``.

    ``None`` selects the default matrix.  An explicit policy is the caller's
    already-resolved class policy; one written for a different class is
    rejected rather than silently applied.
    """
    if policy is None:
        return policy_for(kind)
    if not isinstance(policy, MemoryClassPolicy):
        raise ValueError("policy must be a MemoryClassPolicy")
    if policy.memory_class is not kind:
        raise ValueError(
            f"policy for {policy.memory_class.value!r} cannot govern {kind.value!r}"
        )
    return policy


def memory_class_for_label(label: MemoryLabel | str) -> MemoryClass:
    """Map the WP6 label vocabulary into this broader lifecycle vocabulary."""
    value = label.value if isinstance(label, MemoryLabel) else str(label)
    mapping = {
        MemoryLabel.PROCEDURAL.value: MemoryClass.PROCEDURAL,
        MemoryLabel.FACTUAL.value: MemoryClass.SEMANTIC,
        MemoryLabel.EPISODIC.value: MemoryClass.EPISODIC,
        MemoryLabel.PREFERENCE.value: MemoryClass.PREFERENCE,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError(f"unsupported typed-memory label: {label!r}") from exc


@dataclass(frozen=True)
class WriteDecision:
    disposition: RetrievalDisposition
    memory_class: MemoryClass
    reasons: tuple[str, ...] = ()
    # Revision of the injected ``MemoryPolicy`` that produced this decision;
    # ``None`` when the standalone evaluator resolved the default matrix.
    policy_revision: str | None = None

    @property
    def allowed(self) -> bool:
        return self.disposition is RetrievalDisposition.ALLOW


def evaluate_write(
    memory_class: MemoryClass | str,
    *,
    confidence: float,
    provenance: Sequence[str] = (),
    evidence_count: int = 0,
    explicit_confirmation: bool = False,
    privacy: PrivacyClass | str = PrivacyClass.PUBLIC,
    content: str = "memory",
    policy: MemoryClassPolicy | None = None,
) -> WriteDecision:
    """Evaluate admission without writing anything.

    Model assertions alone do not satisfy classes whose policy requires
    provenance and evidence/explicit confirmation.  Secret content is always
    rejected at this boundary; callers must use the credential/secret paths.

    ``policy`` is the class policy to enforce; when omitted the default
    matrix is used.  It must be the policy for ``memory_class``.
    """
    kind = memory_class if isinstance(memory_class, MemoryClass) else MemoryClass(memory_class)
    policy = _resolve_class_policy(kind, policy)
    reasons: list[str] = []
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        reasons.append("confidence must be finite")
    elif not 0.0 <= confidence <= 1.0:
        reasons.append("confidence must be between 0 and 1")
    elif confidence < policy.write_min_confidence:
        reasons.append("confidence below class threshold")
    if policy.write_requires_provenance and not tuple(p for p in provenance if str(p).strip()):
        reasons.append("provenance is required")
    if policy.write_requires_explicit_or_evidence and evidence_count <= 0 and not explicit_confirmation:
        reasons.append("evidence or explicit confirmation is required")
    try:
        privacy_kind = privacy if isinstance(privacy, PrivacyClass) else PrivacyClass(privacy)
    except ValueError:
        reasons.append("unknown privacy class")
        privacy_kind = PrivacyClass.SECRET
    if privacy_kind is PrivacyClass.SECRET:
        reasons.append("secret content is not memory-store eligible")
    if not isinstance(content, str) or not content.strip():
        reasons.append("content must not be empty")
    return WriteDecision(
        RetrievalDisposition.EXCLUDE if reasons else RetrievalDisposition.ALLOW,
        kind,
        tuple(reasons),
    )


@dataclass(frozen=True)
class RetrievalDecision:
    memory_id: str
    disposition: RetrievalDisposition
    score_components: Mapping[str, float]
    provenance: tuple[str, ...]
    freshness: float
    confidence: float
    exclusion_reasons: tuple[str, ...] = ()
    # See ``WriteDecision.policy_revision``.
    policy_revision: str | None = None

    @property
    def included(self) -> bool:
        return self.disposition is RetrievalDisposition.ALLOW


def evaluate_retrieval(
    memory_id: str,
    memory_class: MemoryClass | str,
    *,
    score_components: Mapping[str, float] | None = None,
    provenance: Sequence[str] = (),
    confidence: float = 0.0,
    freshness: float = 1.0,
    privacy: PrivacyClass | str = PrivacyClass.PUBLIC,
    requested_scope: str | None = None,
    temporal: "TemporalTruth | None" = None,
    now: datetime | None = None,
    include_stale: bool = False,
    policy: MemoryClassPolicy | None = None,
) -> RetrievalDecision:
    """Return an explained, class-aware retrieval decision.

    ``policy`` is the class policy to enforce; when omitted the default
    matrix is used.  It must be the policy for ``memory_class``.
    """
    memory_id = _nonempty(memory_id, "memory_id")
    kind = memory_class if isinstance(memory_class, MemoryClass) else MemoryClass(memory_class)
    policy = _resolve_class_policy(kind, policy)
    reasons: list[str] = []
    if not policy.retrieval_enabled:
        reasons.append("retrieval disabled for class")
    if policy.retrieval_requires_scope and not requested_scope:
        reasons.append("retrieval scope required")
    try:
        privacy_kind = privacy if isinstance(privacy, PrivacyClass) else PrivacyClass(privacy)
    except ValueError:
        privacy_kind = PrivacyClass.SECRET
        reasons.append("unknown privacy class")
    if privacy_kind is PrivacyClass.SECRET:
        reasons.append("secret content is never retrievable")
    if policy.private_by_default and privacy_kind is PrivacyClass.PRIVATE and not requested_scope:
        reasons.append("private memory requires an explicit scope")
    if not 0.0 <= confidence <= 1.0 or not math.isfinite(confidence):
        reasons.append("confidence is invalid")
    if not 0.0 <= freshness <= 1.0 or not math.isfinite(freshness):
        reasons.append("freshness is invalid")
    if temporal is not None and not temporal.is_valid_at(now):
        reasons.append("outside temporal validity interval")
    if not include_stale and policy.default_max_age is not None and freshness <= 0.0:
        reasons.append("stale memory excluded")
    return RetrievalDecision(
        memory_id=memory_id,
        disposition=RetrievalDisposition.EXCLUDE if reasons else RetrievalDisposition.ALLOW,
        score_components=dict(score_components or {}),
        provenance=tuple(str(item) for item in provenance if str(item).strip()),
        freshness=freshness,
        confidence=confidence,
        exclusion_reasons=tuple(reasons),
    )


def _policy_revision(policies: Mapping[MemoryClass, MemoryClassPolicy]) -> str:
    """Content digest of a complete per-class matrix."""
    payload = [
        {
            "memory_class": kind.value,
            "write_min_confidence": float(item.write_min_confidence),
            "write_requires_provenance": bool(item.write_requires_provenance),
            "write_requires_explicit_or_evidence": bool(
                item.write_requires_explicit_or_evidence
            ),
            "retrieval_enabled": bool(item.retrieval_enabled),
            "retrieval_requires_scope": bool(item.retrieval_requires_scope),
            "default_max_age_seconds": (
                None if item.default_max_age is None
                else item.default_max_age.total_seconds()
            ),
            "promotion_enabled": bool(item.promotion_enabled),
            "export_allowed": bool(item.export_allowed),
            "deletion_allowed": bool(item.deletion_allowed),
            "private_by_default": bool(item.private_by_default),
        }
        for kind, item in sorted(policies.items(), key=lambda pair: pair[0].value)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryPolicy:
    """One injected, immutable per-class policy matrix.

    Inspection (``for_class``), admission (``write``) and recall
    (``retrieve``) all resolve the class policy from this instance's own
    mapping, so an injected override governs enforcement and two instances
    never affect each other.  The mapping is copied and frozen at
    construction and must cover every ``MemoryClass``: an incomplete mapping
    is rejected instead of falling back to defaults.  Use ``with_overrides``
    to inherit the default matrix explicitly.
    """

    policies: Mapping[MemoryClass, MemoryClassPolicy] = field(
        default_factory=lambda: MEMORY_CLASS_POLICIES
    )
    # Content digest of the effective matrix, stamped on every decision.
    revision: str = field(init=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policies, Mapping):
            raise ValueError("policies must be a mapping of MemoryClass to MemoryClassPolicy")
        frozen: dict[MemoryClass, MemoryClassPolicy] = {}
        for key, value in self.policies.items():
            if not isinstance(key, MemoryClass):
                raise ValueError(f"policy key must be a MemoryClass: {key!r}")
            if not isinstance(value, MemoryClassPolicy):
                raise ValueError(f"policy for {key.value!r} must be a MemoryClassPolicy")
            if value.memory_class is not key:
                raise ValueError(
                    f"policy for {value.memory_class.value!r} is registered under {key.value!r}"
                )
            frozen[key] = value
        missing = sorted(kind.value for kind in MemoryClass if kind not in frozen)
        if missing:
            raise ValueError("policies must cover every memory class; missing: " + ", ".join(missing))
        # The dataclass is frozen, so the defensive copy is installed directly.
        object.__setattr__(self, "policies", MappingProxyType(frozen))
        object.__setattr__(self, "revision", _policy_revision(frozen))

    def __reduce__(self) -> tuple[Any, ...]:
        # ``mappingproxy`` cannot be copied or pickled; rebuild from a plain
        # mapping so the copy is re-validated and re-frozen.
        return (type(self), (dict(self.policies),))

    @classmethod
    def with_overrides(cls, *overrides: MemoryClassPolicy) -> MemoryPolicy:
        """Build a policy that inherits the default matrix except ``overrides``.

        Each override names its own class through ``memory_class``.
        """
        merged: dict[MemoryClass, MemoryClassPolicy] = dict(MEMORY_CLASS_POLICIES)
        seen: set[MemoryClass] = set()
        for item in overrides:
            if not isinstance(item, MemoryClassPolicy):
                raise ValueError("overrides must be MemoryClassPolicy instances")
            if item.memory_class in seen:
                raise ValueError(f"duplicate override for {item.memory_class.value!r}")
            seen.add(item.memory_class)
            merged[item.memory_class] = item
        return cls(merged)

    def for_class(self, memory_class: MemoryClass | str) -> MemoryClassPolicy:
        kind = memory_class if isinstance(memory_class, MemoryClass) else MemoryClass(memory_class)
        try:
            return self.policies[kind]
        except KeyError as exc:
            raise ValueError(f"unknown memory class: {memory_class!r}") from exc

    def write(self, memory_class: MemoryClass | str, **kwargs: Any) -> WriteDecision:
        # The standalone function stays the single evaluator; this instance
        # only supplies the class policy it resolved from its own mapping.
        decision = evaluate_write(
            memory_class, policy=self.for_class(memory_class), **kwargs
        )
        return replace(decision, policy_revision=self.revision)

    def retrieve(
        self, memory_id: str, memory_class: MemoryClass | str, **kwargs: Any
    ) -> RetrievalDecision:
        decision = evaluate_retrieval(
            memory_id, memory_class, policy=self.for_class(memory_class), **kwargs
        )
        return replace(decision, policy_revision=self.revision)


@dataclass(frozen=True)
class TemporalTruth:
    """Validity and relationship metadata for one assertion."""

    valid_from: datetime
    valid_until: datetime | None = None
    supersedes: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    source_trust: float = 0.5
    confidence: float = 0.0
    last_revalidated_at: datetime | None = None
    decay_half_life: timedelta | None = timedelta(days=180)

    def __post_init__(self) -> None:
        start = _utc(self.valid_from)
        end = None if self.valid_until is None else _utc(self.valid_until)
        if end is not None and end < start:
            raise ValueError("valid_until must not precede valid_from")
        if any(not isinstance(item, str) or not item.strip() for item in (*self.supersedes, *self.contradicts)):
            raise ValueError("temporal relationships require non-empty ids")
        if set(self.supersedes) & set(self.contradicts):
            raise ValueError("a relation cannot be both superseded and contradicted")
        if not 0.0 <= self.source_trust <= 1.0 or not math.isfinite(self.source_trust):
            raise ValueError("source_trust must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0 or not math.isfinite(self.confidence):
            raise ValueError("confidence must be between 0 and 1")
        if self.decay_half_life is not None and self.decay_half_life <= timedelta(0):
            raise ValueError("decay_half_life must be positive")

    def is_valid_at(self, when: datetime | None = None) -> bool:
        point = _utc(when)
        return _utc(self.valid_from) <= point and (
            self.valid_until is None or point < _utc(self.valid_until)
        )

    def decay(self, now: datetime | None = None) -> float:
        if self.decay_half_life is None:
            return 1.0
        age = max(timedelta(0), _utc(now) - _utc(self.last_revalidated_at or self.valid_from))
        return 0.5 ** (age.total_seconds() / self.decay_half_life.total_seconds())

    def revalidate(self, at: datetime | None = None, *, confidence: float | None = None) -> "TemporalTruth":
        value = self.confidence if confidence is None else confidence
        return replace(self, last_revalidated_at=_utc(at), confidence=value)

    def validate_relationships(self, memory_id: str) -> None:
        if memory_id in self.supersedes or memory_id in self.contradicts:
            raise ValueError("a memory cannot relate to itself")
        if set(self.supersedes) & set(self.contradicts):
            raise ValueError("a relation cannot be both superseded and contradicted")


@dataclass(frozen=True)
class EmbeddingIdentity:
    """All metadata required to compare vectors in one serving space."""

    model: str
    revision: str
    dimensions: int
    normalization: str
    truncation: str
    serving_implementation: str

    def __post_init__(self) -> None:
        for name in ("model", "revision", "normalization", "truncation", "serving_implementation"):
            _nonempty(getattr(self, name), name)
        if not isinstance(self.dimensions, int) or self.dimensions <= 0:
            raise ValueError("dimensions must be a positive integer")

    @property
    def key(self) -> tuple[str, str, int, str, str, str]:
        return (self.model, self.revision, self.dimensions, self.normalization,
                self.truncation, self.serving_implementation)

    def matches(self, other: "EmbeddingIdentity") -> bool:
        return self.key == other.key

    def validate_vector(self, vector: Sequence[float]) -> None:
        if len(vector) != self.dimensions:
            raise ValueError("embedding dimension does not match identity")
        if not vector or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
            raise ValueError("embedding must contain finite numeric values")
        if self.normalization == "l2":
            norm = math.sqrt(sum(float(x) * float(x) for x in vector))
            if norm == 0.0 or abs(norm - 1.0) > 1e-4:
                raise ValueError("l2 embedding must be unit normalized")


@dataclass(frozen=True)
class EmbeddingBinding:
    identity: EmbeddingIdentity
    vector_digest: str

    @classmethod
    def from_vector(cls, identity: EmbeddingIdentity, vector: Sequence[float]) -> "EmbeddingBinding":
        identity.validate_vector(vector)
        payload = ",".join(format(float(item), ".17g") for item in vector).encode("ascii")
        return cls(identity, hashlib.sha256(payload).hexdigest())


@dataclass(frozen=True)
class ProceduralPromotionLink:
    """Auditable bridge from typed memory to a candidate skill rollout."""

    memory_id: str
    candidate_id: str
    source_interaction_ids: tuple[str, ...]
    baseline_digest: str
    candidate_digest: str
    rollback_reference: str
    disposition: PromotionDisposition = PromotionDisposition.CANDIDATE
    linked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("memory_id", "candidate_id", "baseline_digest", "candidate_digest", "rollback_reference"):
            _nonempty(getattr(self, name), name)
        if not self.source_interaction_ids or any(not item.strip() for item in self.source_interaction_ids):
            raise ValueError("promotion linkage requires source interactions")


def link_procedural_promotion(
    memory: TypedMemory,
    *,
    candidate_id: str,
    source_interaction_ids: Iterable[str],
    baseline_digest: str,
    candidate_digest: str,
    rollback_reference: str,
) -> ProceduralPromotionLink:
    """Create linkage only for evidence-backed procedural memory."""
    if memory.label is not MemoryLabel.PROCEDURAL:
        raise ValueError("only procedural memories may become skill candidates")
    if not memory.evidence:
        raise ValueError("procedural promotion requires evidence")
    if memory.contradictions:
        raise ValueError("contradictory procedural memory cannot be promoted")
    return ProceduralPromotionLink(
        memory_id=memory.memory_id,
        candidate_id=candidate_id,
        source_interaction_ids=tuple(dict.fromkeys(source_interaction_ids)),
        baseline_digest=baseline_digest,
        candidate_digest=candidate_digest,
        rollback_reference=rollback_reference,
    )
