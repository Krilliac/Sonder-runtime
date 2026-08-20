"""Bounded evaluation for labeled memory retrieval.

The production recall path owns retrieval policy (including the default
cosine floor and MMR ordering).  This module is deliberately read-only: it
evaluates returned identifiers against a caller-supplied labeled set without
performing retrieval, storage, embedding, or model I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Iterable, Mapping, Sequence

from ...domain.memory.rules import DEFAULT_RECALL_MIN_SIM

MAX_EVALUATION_CASES = 10_000
MAX_RESULTS_PER_CASE = 1_000
MAX_CONTEXT_TOKENS_PER_RESULT = 1_000_000


@dataclass(frozen=True)
class RetrievedMemory:
    """One returned memory and the labels needed for safety evaluation."""

    memory_id: str
    is_contradictory: bool = False
    is_stale: bool = False
    context_tokens: int = 0


@dataclass(frozen=True)
class RetrievalLabel:
    """Gold labels for one query/retrieval result pair."""

    query_id: str
    relevant_ids: frozenset[str]
    contradictory_ids: frozenset[str] = frozenset()
    stale_ids: frozenset[str] = frozenset()
    retrieved: tuple[RetrievedMemory, ...] = ()
    latency_ms: float = 0.0


@dataclass(frozen=True)
class RetrievalMetrics:
    """Bounded aggregate metrics; every ratio is in the inclusive [0, 1]."""

    cases: int
    evaluated_results: int
    relevant_precision: float
    relevant_recall: float
    contradiction_rate: float
    stale_recall: float
    latency_p95_ms: float
    context_cost_tokens: float
    default_min_similarity: float = DEFAULT_RECALL_MIN_SIM

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cases": self.cases,
            "evaluated_results": self.evaluated_results,
            "relevant_precision": self.relevant_precision,
            "relevant_recall": self.relevant_recall,
            "contradiction_rate": self.contradiction_rate,
            "stale_recall": self.stale_recall,
            "latency_p95_ms": self.latency_p95_ms,
            "context_cost_tokens": self.context_cost_tokens,
            "default_min_similarity": self.default_min_similarity,
        }


def evaluate_retrieval(
    labels: Iterable[RetrievalLabel], *, k: int | None = None
) -> RetrievalMetrics:
    """Evaluate labeled results with bounded, deterministic aggregate metrics.

    ``contradiction_rate`` is the share of returned results labeled
    contradictory. ``stale_recall`` is the share of gold stale IDs returned;
    unlike contradiction rate, it is recall against the stale case set and is
    zero when no stale labels exist.  Duplicate returned IDs are counted once
    per query, matching retrieval identity rather than token count.
    """
    cases = tuple(labels)
    if len(cases) > MAX_EVALUATION_CASES:
        raise ValueError("too many retrieval evaluation cases")
    if k is not None and (not isinstance(k, int) or k <= 0 or k > MAX_RESULTS_PER_CASE):
        raise ValueError("k must be between 1 and MAX_RESULTS_PER_CASE")

    total_results = total_relevant = total_expected = 0
    total_contradictory = total_stale_hits = total_stale_expected = 0
    total_context = 0
    latencies: list[float] = []

    for case in cases:
        if not case.query_id:
            raise ValueError("query_id must not be empty")
        if len(case.retrieved) > MAX_RESULTS_PER_CASE:
            raise ValueError("too many results in one retrieval case")
        selected = case.retrieved[:k] if k is not None else case.retrieved
        ids = {item.memory_id for item in selected}
        total_results += len(ids)
        total_relevant += len(ids & case.relevant_ids)
        total_expected += len(case.relevant_ids)
        total_contradictory += len(
            ids & case.contradictory_ids
        ) + sum(item.is_contradictory for item in selected if item.memory_id not in case.contradictory_ids)
        total_stale_hits += len(ids & case.stale_ids)
        total_stale_expected += len(case.stale_ids)
        total_context += sum(
            min(MAX_CONTEXT_TOKENS_PER_RESULT, max(0, item.context_tokens))
            for item in selected
        )
        if case.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        latencies.append(float(case.latency_ms))

    def ratio(numerator: int, denominator: int) -> float:
        return 0.0 if denominator == 0 else min(1.0, max(0.0, numerator / denominator))

    latencies.sort()
    p95 = 0.0 if not latencies else latencies[max(0, ceil(len(latencies) * 0.95) - 1)]
    return RetrievalMetrics(
        cases=len(cases),
        evaluated_results=total_results,
        relevant_precision=ratio(total_relevant, total_results),
        relevant_recall=ratio(total_relevant, total_expected),
        contradiction_rate=ratio(total_contradictory, total_results),
        stale_recall=ratio(total_stale_hits, total_stale_expected),
        latency_p95_ms=p95,
        context_cost_tokens=(
            min(MAX_CONTEXT_TOKENS_PER_RESULT, total_context / total_results)
            if total_results else 0.0
        ),
    )


def evaluate_labeled_mapping(
    cases: Mapping[str, RetrievalLabel], *, k: int | None = None
) -> RetrievalMetrics:
    """Convenience wrapper for keyed evaluation datasets."""
    return evaluate_retrieval(cases.values(), k=k)
