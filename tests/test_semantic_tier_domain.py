"""Tests for the pure semantic tier centroid classifier."""

from __future__ import annotations

import math

import pytest

from sonder_runtime.domain.routing.semantic_tier import (
    BANK_DIGEST,
    EXAMPLE_BANK,
    MIN_MARGIN,
    SemanticDecision,
    build_centroids,
    classify_vector,
)


def test_example_bank_contains_six_bank_examples_per_tier() -> None:
    assert set(EXAMPLE_BANK) == {"fast", "general", "code", "reasoning", "vision"}
    assert all(6 <= len(prompts) <= 12 for prompts in EXAMPLE_BANK.values())
    assert all(prompt.strip() for prompts in EXAMPLE_BANK.values() for prompt in prompts)
    assert len(BANK_DIGEST) == 64


def test_build_centroids_normalizes_examples_and_mean() -> None:
    centroids = build_centroids(
        {"a": ((3.0, 4.0), (1.0, 0.0)), "b": ((0.0, 1.0),)}
    )
    expected = ((3.0 / 5.0 + 1.0) / 2, (4.0 / 5.0) / 2)
    norm = math.sqrt(sum(value * value for value in expected))
    assert centroids["a"] == pytest.approx(tuple(value / norm for value in expected))
    assert centroids["b"] == pytest.approx((0.0, 1.0))


def test_classify_vector_returns_top_score_and_margin() -> None:
    decision = classify_vector((1.0, 0.0), {"fast": (1.0, 0.0), "code": (0.0, 1.0)})
    assert isinstance(decision, SemanticDecision)
    assert decision.tier == "fast"
    assert decision.margin == pytest.approx(1.0)
    assert decision.scores == {"fast": pytest.approx(1.0), "code": pytest.approx(0.0)}


def test_singleton_centroid_has_no_meaningful_margin() -> None:
    decision = classify_vector((1.0, 0.0), {"fast": (1.0, 0.0)})
    assert decision.tier == "fast"
    assert decision.margin == 0.0


def test_classify_vector_preserves_small_margin_for_caller_gate() -> None:
    decision = classify_vector(
        (1.0, 0.0),
        {"fast": (1.0, 0.0), "general": (0.998, 0.0632455532)},
    )
    assert decision.tier == "fast"
    assert decision.margin < MIN_MARGIN


def test_build_centroids_rejects_malformed_vectors() -> None:
    with pytest.raises(ValueError):
        build_centroids({"a": ((0.0, 0.0),)})
    with pytest.raises(ValueError):
        build_centroids({"a": ((1.0, 0.0),), "b": ((1.0,),)})
    with pytest.raises(ValueError):
        build_centroids({"a": ((math.inf, 0.0),)})
    with pytest.raises(ValueError):
        build_centroids({"a": ((True, 0.0),)})
    with pytest.raises(ValueError):
        build_centroids({"a": (("not-a-number", 0.0),)})


def test_normalization_handles_large_finite_vectors() -> None:
    centroids = build_centroids({"a": ((1e308, 1e308),), "b": ((-1e308, 1e308),)})
    assert centroids["a"] == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))
    assert classify_vector((1e308, 1e308), centroids).tier == "a"


def test_classify_vector_rejects_malformed_query_or_centroid() -> None:
    with pytest.raises(ValueError):
        classify_vector((0.0, 0.0), {"a": (1.0, 0.0)})
    with pytest.raises(ValueError):
        classify_vector((1.0, 0.0), {})


def test_deterministic_fake_embedding_classifies_bank_and_held_out_paraphrases() -> None:
    feature_words = (
        ("fast", ("ok", "hey", "awesome", "cheers", "passed", "tomorrow")),
        ("general", ("difference", "birthday", "rewrite", "story", "summarize", "questions")),
        ("code", ("error", "refactor", "implement", "optimize", "python", "git")),
        ("reasoning", ("train", "ways", "probability", "sheep", "derive", "prime")),
        ("vision", ("plant", "outfit", "font", "logo", "dish", "floor", "photo")),
    )

    def embed(text: str) -> tuple[float, ...]:
        lowered = text.lower()
        return tuple(sum(lowered.count(word) for word in words) for _, words in feature_words)

    centroids = build_centroids(
        {tier: tuple(embed(prompt) for prompt in prompts) for tier, prompts in EXAMPLE_BANK.items()}
    )
    held_out = (
        ("hey, please give me a quick friendly acknowledgement", "fast"),
        ("summarize the main idea in this passage", "general"),
        ("debug this Python exception", "code"),
        ("show the reasoning behind this probability answer", "reasoning"),
        ("describe what is visible in this photo", "vision"),
    )
    correct = sum(classify_vector(embed(prompt), centroids).tier == expected for prompt, expected in held_out)
    assert correct / len(held_out) >= 0.8
