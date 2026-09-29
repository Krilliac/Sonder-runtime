"""Opt-in, local-only end-to-end accuracy on the unchanged bake-off test split.

Run: python -m pytest tests/test_semantic_tier_live.py --run-network --run-model -s
No model is downloaded. The fixture is the 2026-09-28 task_b.jsonl (150 rows:
30 bank, 120 test); only the 96 text-tier test rows contribute to accuracy.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

import tier_router
import sonder_runtime.adapters.embeddings as embeddings
import sonder_runtime.adapters.semantic_tier as adapter
from sonder_runtime.domain.routing.semantic_tier import EXAMPLE_BANK


DATA = Path(__file__).with_name("fixtures") / "semantic_tier_task_b.jsonl"


def _rows():
    with DATA.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def test_bakeoff_fixture_preserves_splits_and_exact_bank():
    rows = _rows()
    bank = {(row["label"], row["text"]) for row in rows if row["split"] == "bank"}
    held_out = {(row["label"], row["text"]) for row in rows if row["split"] == "test"}
    assert bank == {(tier, text) for tier, texts in EXAMPLE_BANK.items() for text in texts}
    assert len(bank) == 30
    assert len(held_out) == 120
    assert not {text for _, text in bank} & {text for _, text in held_out}


@pytest.mark.network
@pytest.mark.model
def test_local_ollama_router_text_accuracy(request, monkeypatch):
    if not (request.config.getoption("--run-network", default=False)
            and request.config.getoption("--run-model", default=False)):
        pytest.skip("requires explicit --run-network --run-model opt-in")

    # The test never follows ambient remote endpoints or changes remote consent.
    base = "http://127.0.0.1:11434"
    model = "nomic-embed-text:latest"
    probe = embeddings.embed("classification: ok cool", timeout=5, base=base, model=model)
    if probe is None:
        pytest.skip("loopback Ollama with nomic-embed-text is not reachable/allowed")
    monkeypatch.setattr(embeddings, "BASE", base)
    monkeypatch.setattr(embeddings, "EMBED_MODEL", model)

    # Cold startup is deliberately fail-soft. Wait for bounded warmup outside
    # the accuracy measurement; each individual routing call still has 1 s.
    deadline = time.monotonic() + 40
    warm = None
    while warm is None and time.monotonic() < deadline:
        warm = adapter.semantic_signal("ok cool")
        if warm is None:
            time.sleep(0.1)
    assert warm is not None, "reachable embedding model could not warm the semantic bank"

    examples = [row for row in _rows() if row["split"] == "test" and row["label"] != "vision"]
    assert len(examples) == 96
    available = {"fast", "general", "code", "reasoning", "cloud-general", "vision"}
    outcomes = []
    for row in examples:
        # Real messages arrive seconds apart. One in-flight embedding at a time
        # is deliberate (a slow embedder never stalls chat past 1 s), so pace
        # the benchmark rather than measure calls skipped behind a busy flight.
        waited = time.monotonic()
        while adapter._flight is not None and time.monotonic() - waited < 10:
            time.sleep(0.02)
        outcomes.append(tier_router.route(row["text"], available, semantic_enabled=True))
    correct = sum(result["tier"] == row["label"] for row, result in zip(examples, outcomes, strict=True))
    assert all(result["tier"] != "vision" for result in outcomes)
    semantic_count = sum(result["signal"] == "semantic" for result in outcomes)
    print(f"lexical-first hybrid: {correct}/96 accuracy={correct / 96:.3f}; semantic={semantic_count}")
    assert correct / len(examples) >= 0.75
