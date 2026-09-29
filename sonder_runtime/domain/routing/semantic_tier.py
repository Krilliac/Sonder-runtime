"""Pure centroid-based semantic tier classification.

This module deliberately accepts vectors rather than embedding text.  Runtime
adapters own embedding and I/O policy; this domain layer only normalizes,
scores, and reports a top tier with its cosine-score margin.

``MIN_MARGIN`` is a provisional 0.05 gate pending calibration against live
local embeddings; callers should accept a semantic result only when the top
score exceeds the runner-up by at least this amount.  The bank is the
six-example ``bank`` split from ``router-bakeoff/data/task_b.jsonl`` for each
tier; test examples are not included.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

MIN_MARGIN: Final = 0.05

EXAMPLE_BANK: Final[dict[str, tuple[str, ...]]] = {
    "fast": (
        "ok cool",
        "hey Sonder",
        "that's awesome",
        "cheers",
        "I passed my exam!",
        "see you tomorrow",
    ),
    "general": (
        "what's the difference between a virus and a bacterium?",
        "write a birthday message for my mom",
        "rewrite this to sound more formal: we gotta ship it soon",
        "write a short story about a lonely lighthouse keeper",
        "summarize this: The city council voted 7-2 to expand bike lanes downtown next year.",
        "what are good questions to ask at the end of a job interview?",
    ),
    "code": (
        "fix this error: TypeError: 'NoneType' object is not subscriptable",
        "refactor this class to use dependency injection",
        "implement binary search in Rust",
        "optimize this loop: for i in range(len(a)): total += a[i]",
        "my Python script hangs forever on queue.get(), why?",
        "what does git rebase -i do?",
    ),
    "reasoning": (
        "A train leaves at 3pm going 60 mph; another leaves the same station at 4pm "
        "going 80 mph. When does the second catch the first?",
        "how many distinct ways can the letters of BANANA be arranged?",
        "what's the probability of getting at least one six in four rolls of a fair die?",
        "A farmer has 17 sheep and all but 9 run away. How many are left? Walk through it.",
        "derive the quadratic formula from ax^2+bx+c=0",
        "is 1001 prime? show your reasoning",
    ),
    "vision": (
        "identify the plant in this photo",
        "rate my outfit in this pic",
        "what font is used in this logo image?",
        "which of these two product photos looks more professional?",
        "what dish is in this picture, and how would I cook it?",
        "describe the floor plan in this image",
    ),
}

_BANK_PAYLOAD = json.dumps(EXAMPLE_BANK, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
BANK_DIGEST: Final[str] = hashlib.sha256(_BANK_PAYLOAD.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticDecision:
    """Top centroid tier, score margin, and all cosine scores."""

    tier: str
    margin: float
    scores: Mapping[str, float]


def _normalise(vector: Sequence[float], *, name: str) -> tuple[float, ...]:
    try:
        raw_values = tuple(vector)
        if any(isinstance(value, bool) for value in raw_values):
            raise ValueError("boolean values are not vectors")
        values = tuple(float(value) for value in raw_values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain numeric values") from exc
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} must be non-empty and finite")
    scale = max(abs(value) for value in values)
    if scale == 0.0:
        raise ValueError(f"{name} must not be the zero vector")
    scaled = tuple(value / scale for value in values)
    norm = math.sqrt(sum(value * value for value in scaled))
    return tuple(value / norm for value in scaled)


def build_centroids(vectors_by_tier: Mapping[str, Sequence[Sequence[float]]]) -> dict[str, tuple[float, ...]]:
    """Return normalized mean vectors for each tier's example vectors."""
    if not vectors_by_tier:
        raise ValueError("at least one tier is required")
    centroids: dict[str, tuple[float, ...]] = {}
    dimension: int | None = None
    for tier, vectors in vectors_by_tier.items():
        if not tier or not vectors:
            raise ValueError("each tier needs at least one vector")
        normalized = tuple(_normalise(vector, name=f"{tier} vector") for vector in vectors)
        vector_dimension = len(normalized[0])
        if dimension is None:
            dimension = vector_dimension
        if vector_dimension != dimension or any(len(vector) != dimension for vector in normalized):
            raise ValueError("all vectors must have the same dimension")
        mean = tuple(sum(vector[index] for vector in normalized) / len(normalized) for index in range(dimension))
        centroids[tier] = _normalise(mean, name=f"{tier} centroid")
    return centroids


def classify_vector(vector: Sequence[float], centroids: Mapping[str, Sequence[float]]) -> SemanticDecision:
    """Choose the centroid with maximum cosine similarity and report its margin."""
    if not centroids:
        raise ValueError("at least one centroid is required")
    query = _normalise(vector, name="query vector")
    scores: dict[str, float] = {}
    for tier, centroid in centroids.items():
        normalized_centroid = _normalise(centroid, name=f"{tier} centroid")
        if len(normalized_centroid) != len(query):
            raise ValueError("query and centroids must have the same dimension")
        scores[tier] = sum(left * right for left, right in zip(query, normalized_centroid, strict=True))
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    margin = ranked[0][1] - ranked[1][1] if len(ranked) > 1 else 0.0
    return SemanticDecision(tier=ranked[0][0], margin=margin, scores=scores)


# A trained linear head (logistic regression over L2-normalised embeddings),
# the measured upgrade over centroids: 92.5% vs 84.2% on the bake-off's five-way
# test split with nomic-embed-text (router-bakeoff/results.md). Inference is a
# dot product plus softmax; the margin is top probability minus runner-up.
HEAD_MIN_MARGIN: Final = 0.3


@dataclass(frozen=True)
class LinearHead:
    labels: tuple[str, ...]
    weights: tuple[tuple[float, ...], ...]
    bias: tuple[float, ...]
    embedding_model: str
    input_prefix: str


def load_head(payload: Mapping) -> LinearHead:
    """Validate an exported head (``labels``, ``W``, ``b``, ``embedding_model``)."""
    labels = tuple(str(label) for label in payload["labels"])
    weights = tuple(tuple(float(value) for value in row) for row in payload["W"])
    bias = tuple(float(value) for value in payload["b"])
    if len(labels) < 2 or len(set(labels)) != len(labels):
        raise ValueError("a head needs at least two distinct labels")
    if len(weights) != len(labels) or len(bias) != len(labels):
        raise ValueError("head rows must match its labels")
    dimension = len(weights[0])
    if not dimension or any(len(row) != dimension for row in weights):
        raise ValueError("head rows must share one dimension")
    if not all(math.isfinite(value) for row in weights for value in row) or not all(
            math.isfinite(value) for value in bias):
        raise ValueError("head values must be finite")
    model = str(payload.get("embedding_model") or "")
    if not model:
        raise ValueError("head must name its embedding model")
    return LinearHead(labels, weights, bias, model, str(payload.get("input_prefix") or ""))


def classify_with_head(vector: Sequence[float], head: LinearHead) -> SemanticDecision:
    """Softmax over ``W . v + b`` for the L2-normalised query vector."""
    query = _normalise(vector, name="query vector")
    if len(query) != len(head.weights[0]):
        raise ValueError("query and head must have the same dimension")
    logits = [sum(w * q for w, q in zip(row, query, strict=True)) + b
              for row, b in zip(head.weights, head.bias, strict=True)]
    top = max(logits)
    exps = [math.exp(value - top) for value in logits]
    total = sum(exps)
    scores = {label: value / total for label, value in zip(head.labels, exps, strict=True)}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return SemanticDecision(tier=ranked[0][0], margin=ranked[0][1] - ranked[1][1], scores=scores)
