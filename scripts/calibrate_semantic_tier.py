"""Bank-only leave-one-out calibration for semantic tier routing.

This utility is opt-in and advisory.  It never rewrites the router threshold
and never embeds the bake-off test split.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.domain.routing.semantic_tier import (
    BANK_DIGEST,
    EXAMPLE_BANK,
    build_centroids,
    classify_vector,
)

DEFAULT_THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1)
Embedder = Callable[..., Sequence[float] | None]


def embed_bank(
    prompts_by_tier: Mapping[str, Sequence[str]],
    *,
    embedder: Embedder,
    timeout: float,
    model: str,
) -> dict[str, tuple[Sequence[float], ...]] | None:
    """Embed every supplied bank prompt once, returning None on any failure."""
    vectors: dict[str, tuple[Sequence[float], ...]] = {}
    try:
        for tier, prompts in prompts_by_tier.items():
            tier_vectors = []
            for prompt in prompts:
                vector = embedder(prompt, timeout=timeout, model=model)
                if vector is None:
                    return None
                tier_vectors.append(vector)
            vectors[tier] = tuple(tier_vectors)
    except Exception:
        return None
    return vectors


def evaluate_bank(
    vectors_by_tier: Mapping[str, Sequence[Sequence[float]]],
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
) -> dict[str, object]:
    """Evaluate selective accuracy using leave-one-out bank centroids.

    Vision examples remain competitors, while vision gold examples are omitted
    from ``n`` and the precision/coverage metrics because text-only routing
    cannot claim to solve attached-image requests.
    """
    threshold_values = tuple(sorted({float(value) for value in thresholds}))
    nonvision = tuple(
        (tier, index, vector)
        for tier, vectors in vectors_by_tier.items()
        if tier != "vision"
        for index, vector in enumerate(vectors)
    )
    rows = []
    for threshold in threshold_values:
        accepted = correct = 0
        for gold, held_out_index, vector in nonvision:
            training = {
                tier: tuple(
                    item
                    for index, item in enumerate(vectors)
                    if not (tier == gold and index == held_out_index)
                )
                for tier, vectors in vectors_by_tier.items()
            }
            try:
                decision = classify_vector(vector, build_centroids(training))
            except (TypeError, ValueError):
                continue
            if decision.tier == "vision" or decision.margin < threshold:
                continue
            accepted += 1
            correct += decision.tier == gold
        rows.append(
            {
                "threshold": threshold,
                "accepted": accepted,
                "correct": correct,
                "precision": correct / accepted if accepted else 0.0,
                "coverage": accepted / len(nonvision) if nonvision else 0.0,
            }
        )
    candidate = next(
        (
            row["threshold"]
            for row in rows
            if row["precision"] >= 0.9 and row["coverage"] >= 0.5
        ),
        None,
    )
    return {"n": len(nonvision), "rows": rows, "candidate_threshold": candidate}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="nomic-embed-text")
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args(argv)
    import sonder_runtime.adapters.embeddings as embeddings
    from sonder_runtime.adapters.semantic_tier import _embedding_text
    from sonder_runtime.domain.model_routing import is_cloud_model_name

    base = embeddings.BASE
    model = embeddings.canonical_model_name(args.model)
    if (not embeddings.endpoint_is_loopback(base)
            or is_cloud_model_name(model) or model.startswith("cloud")):
        print(json.dumps({"error": "embedding endpoint and model must be local-only"}))
        return 2

    prompts = {
        tier: tuple(_embedding_text(prompt, model) for prompt in examples)
        for tier, examples in EXAMPLE_BANK.items()
    }
    vectors = embed_bank(
        prompts,
        embedder=lambda text, **kwargs: embeddings.embed(text, base=base, **kwargs),
        timeout=max(0.01, min(args.timeout, 1.0)),
        model=model,
    )
    output: dict[str, object] = {"model": args.model, "bank_digest": BANK_DIGEST}
    if vectors is None:
        output["error"] = "local embedding failed"
        print(json.dumps(output, sort_keys=True))
        return 1
    output["calibration"] = evaluate_bank(vectors)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
