"""Deterministic, policy-aware hybrid memory retrieval.

This service is deliberately storage-neutral: adapters provide bounded memory
candidates and optional semantic scores. It supplies one explainable ordering
for exact, temporal, decision, and failure retrieval without reaching into the
server or session surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Iterable

from .memory_policy import MemoryClass, PrivacyClass, TemporalTruth, evaluate_retrieval


MAX_CANDIDATES = 10_000
MAX_LIMIT = 100


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", value.casefold(), re.UNICODE))


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory_id: str
    text: str
    memory_class: MemoryClass | str
    created_at: datetime
    confidence: float = 0.0
    freshness: float = 1.0
    privacy: PrivacyClass | str = PrivacyClass.PUBLIC
    project: str | None = None
    entity_id: str | None = None
    temporal: TemporalTruth | None = None
    decision_tags: tuple[str, ...] = ()
    failure_tags: tuple[str, ...] = ()
    semantic_score: float = 0.0
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    mode: str = "hybrid"
    limit: int = 10
    scope: str | None = None
    project: str | None = None
    entity_id: str | None = None
    at: datetime | None = None
    decision_tag: str | None = None
    failure_tag: str | None = None
    include_stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 4096:
            raise ValueError("retrieval query text must be bounded and non-empty")
        if self.mode not in {"hybrid", "exact", "temporal", "decision", "failure", "entity"}:
            raise ValueError("unknown retrieval mode")
        if isinstance(self.limit, bool) or not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    candidate: MemoryCandidate
    score: float
    exact_score: float
    temporal_score: float
    reasons: tuple[str, ...] = ()


class HybridMemoryRetriever:
    def retrieve(self, candidates: Iterable[MemoryCandidate], query: RetrievalQuery) -> tuple[RetrievedMemory, ...]:
        rows: list[MemoryCandidate] = []
        for candidate in candidates:
            rows.append(candidate)
            if len(rows) > MAX_CANDIDATES:
                break
        if len(rows) > MAX_CANDIDATES:
            raise ValueError("too many memory candidates")
        query_tokens = _tokens(query.text)
        now = query.at or datetime.now(timezone.utc)
        selected: list[RetrievedMemory] = []
        for candidate in rows:
            # A caller must name the exact project before project-scoped
            # memories can enter a result, including for semantic search.
            if candidate.project is not None and candidate.project != query.project:
                continue
            if query.mode == "entity" and (not query.entity_id or candidate.entity_id != query.entity_id):
                continue
            if query.mode == "decision" and (not query.decision_tag or query.decision_tag not in candidate.decision_tags):
                continue
            if query.mode == "failure" and (not query.failure_tag or query.failure_tag not in candidate.failure_tags):
                continue
            exact = len(query_tokens & _tokens(candidate.text)) / max(1, len(query_tokens))
            if query.mode == "exact" and exact <= 0.0:
                continue
            if query.mode == "decision" and query.decision_tag not in candidate.decision_tags:
                continue
            if query.mode == "failure" and query.failure_tag not in candidate.failure_tags:
                continue
            temporal_score = candidate.temporal.decay(now) if candidate.temporal else 1.0
            if query.mode == "temporal" and candidate.temporal is None:
                continue
            decision = evaluate_retrieval(
                candidate.memory_id,
                candidate.memory_class,
                score_components={"exact": exact, "semantic": candidate.semantic_score},
                provenance=candidate.provenance,
                confidence=candidate.confidence,
                freshness=candidate.freshness,
                privacy=candidate.privacy,
                requested_scope=query.scope,
                temporal=candidate.temporal,
                now=now,
                include_stale=query.include_stale,
            )
            if not decision.included:
                continue
            score = (0.55 * exact) + (0.25 * max(0.0, min(1.0, candidate.semantic_score))) + (0.10 * temporal_score) + (0.10 * candidate.confidence)
            selected.append(RetrievedMemory(candidate, score, exact, temporal_score))
        selected.sort(key=lambda item: (-item.score, -item.exact_score, -item.temporal_score, -item.candidate.created_at.timestamp(), item.candidate.memory_id))
        return tuple(selected[: query.limit])


__all__ = ["HybridMemoryRetriever", "MAX_CANDIDATES", "MAX_LIMIT", "MemoryCandidate", "RetrievedMemory", "RetrievalQuery"]
