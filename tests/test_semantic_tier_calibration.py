"""Isolated tests for bank-only semantic router calibration."""

from __future__ import annotations

import pytest

from scripts.calibrate_semantic_tier import embed_bank, evaluate_bank


def _basis_bank(close_fast_general: bool = False) -> dict[str, tuple[tuple[float, ...], ...]]:
    basis = {
        "fast": (1.0, 0.0, 0.0, 0.0, 0.0),
        "general": (0.0, 1.0, 0.0, 0.0, 0.0),
        "code": (0.0, 0.0, 1.0, 0.0, 0.0),
        "reasoning": (0.0, 0.0, 0.0, 1.0, 0.0),
        "vision": (0.0, 0.0, 0.0, 0.0, 1.0),
    }
    if close_fast_general:
        basis["general"] = (0.999, 0.045, 0.0, 0.0, 0.0)
    return {tier: (vector,) * 6 for tier, vector in basis.items()}


def test_leave_one_out_reports_selective_accuracy_and_candidate() -> None:
    result = evaluate_bank(_basis_bank(), thresholds=(0.0, 0.05, 0.1))
    assert result["n"] == 24
    assert result["rows"][0] == {
        "threshold": 0.0,
        "accepted": 24,
        "correct": 24,
        "precision": 1.0,
        "coverage": 1.0,
    }
    assert result["candidate_threshold"] == 0.0


def test_margin_threshold_rejects_ambiguous_vectors() -> None:
    result = evaluate_bank(_basis_bank(close_fast_general=True), thresholds=(0.05, 0.1))
    assert result["n"] == 24
    assert result["rows"][0]["accepted"] == 12
    assert result["rows"][0]["correct"] == 12
    assert result["rows"][0]["coverage"] == 0.5
    assert result["candidate_threshold"] == 0.05


def test_embed_bank_uses_injected_fake_and_soft_failures_are_reported() -> None:
    calls: list[tuple[str, float, str]] = []

    def fake(prompt: str, *, timeout: float, model: str) -> tuple[float, ...] | None:
        calls.append((prompt, timeout, model))
        return (1.0, 0.0)

    prompts = {"fast": ("hello",), "general": ("explain",)}
    vectors = embed_bank(prompts, embedder=fake, timeout=0.75, model="fake")
    assert vectors == {"fast": ((1.0, 0.0),), "general": ((1.0, 0.0),)}
    assert calls == [("hello", 0.75, "fake"), ("explain", 0.75, "fake")]

    def failing(*_args: object, **_kwargs: object) -> None:
        return None

    assert embed_bank(prompts, embedder=failing, timeout=0.1, model="fake") is None


def test_vision_winner_is_an_abstention():
    bank = _basis_bank()
    bank["fast"] = (bank["vision"][0], *bank["fast"][1:])
    row = evaluate_bank(bank, thresholds=(0.05,))["rows"][0]
    assert row["accepted"] == row["correct"] == 23


@pytest.mark.parametrize("model,prefix", [("nomic-embed-text", "classification: "), ("bge-m3", "")])
def test_calibration_matches_runtime_prefix_and_pins_local_endpoint(monkeypatch, capsys, model, prefix):
    import sonder_runtime.adapters.embeddings as embeddings
    from scripts import calibrate_semantic_tier as calibration

    base = "http://127.0.0.1:11434"
    monkeypatch.setattr(embeddings, "BASE", base)
    vectors = {
        prefix + prompt: _basis_bank()[tier][0]
        for tier, prompts in calibration.EXAMPLE_BANK.items() for prompt in prompts
    }
    calls = []

    def fake(text, **kwargs):
        calls.append((text, kwargs))
        # A concurrent reconfiguration must not move the remaining bank off-box.
        monkeypatch.setattr(embeddings, "BASE", "https://remote.invalid:11434")
        return vectors[text]

    monkeypatch.setattr(embeddings, "embed", fake)
    assert calibration.main(["--model", model]) == 0
    assert len(calls) == 30
    assert all(kwargs["base"] == base for _, kwargs in calls)
    assert all(text in vectors for text, _ in calls)
    assert '"n": 24' in capsys.readouterr().out
