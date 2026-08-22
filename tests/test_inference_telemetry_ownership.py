from __future__ import annotations

from sonder_runtime.adapters.inference.telemetry import (
    from_ollama,
    from_openai_compatible,
)
from sonder_runtime.adapters.inference_telemetry import (
    from_ollama as legacy_from_ollama,
    from_openai_compatible as legacy_from_openai_compatible,
)


def test_legacy_exports_point_at_the_canonical_inference_boundary():
    assert legacy_from_ollama is from_ollama
    assert legacy_from_openai_compatible is from_openai_compatible


def test_ollama_telemetry_normalization_remains_behaviorally_identical():
    telemetry = from_ollama(
        {
            "total_duration": 2_000_000_000,
            "prompt_eval_count": 10,
            "prompt_eval_duration": 500_000_000,
            "eval_count": 20,
            "eval_duration": 1_000_000_000,
            "cold_start": True,
        }
    )
    assert telemetry is not None
    assert telemetry.backend_total_ms == 2000.0
    assert telemetry.prompt_tokens_per_second == 20.0
    assert telemetry.output_tokens_per_second == 20.0
    assert telemetry.load_state == "cold"


def test_openai_compatible_telemetry_keeps_reported_rate_precedence():
    telemetry = from_openai_compatible(
        {
            "timings": {
                "predicted_ms": 500.0,
                "predicted_n": 10,
                "predicted_per_second": 99.0,
            },
            "load_state": "warm",
        }
    )
    assert telemetry is not None
    assert telemetry.output_tokens_per_second == 99.0
    assert telemetry.load_state == "warm"
