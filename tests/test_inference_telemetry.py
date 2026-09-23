from __future__ import annotations

import math

from sonder_runtime.adapters.inference_telemetry import (
    from_ollama,
    from_openai_compatible,
)
from sonder_runtime.application.ports.model_gateway import (
    InferenceTelemetry,
    ModelResponse,
)
from sonder_runtime.platform.metrics import MetricsRegistry


def test_model_response_constructor_remains_backward_compatible():
    response = ModelResponse("text", "model", "tier", 12, 3, 4)
    assert response.telemetry is None


def test_ollama_rates_require_measured_counts_and_durations():
    telemetry = from_ollama({
        "prompt_eval_count": 10,
        "prompt_eval_duration": 0,
        "eval_duration": 1_000_000_000,
    })
    assert telemetry.prompt_tokens_per_second is None
    assert telemetry.output_tokens_per_second is None


def test_ollama_reports_provider_cache_counts_without_inference():
    telemetry = from_ollama({
        "prompt_eval_count": 20,
        "prompt_eval_cached_count": 13,
        "eval_count": 4,
    })
    assert telemetry is not None
    assert telemetry.prompt_tokens == 20
    assert telemetry.prompt_cached_tokens == 13
    assert telemetry.prompt_uncached_tokens == 7
    assert telemetry.output_tokens == 4


def test_ollama_drops_absent_malformed_or_inconsistent_cache_counts():
    assert from_ollama({"prompt_eval_cached_count": 3}) is None
    absent = from_ollama({"prompt_eval_count": 20})
    assert absent is not None
    assert absent.prompt_cached_tokens is None
    malformed = from_ollama({"prompt_eval_count": "20", "prompt_eval_cached_count": 3})
    assert malformed is None
    inconsistent = from_ollama({"prompt_eval_count": 2, "prompt_eval_cached_count": 3})
    assert inconsistent is not None
    assert inconsistent.prompt_tokens == 2
    assert inconsistent.prompt_cached_tokens is None
    assert inconsistent.prompt_uncached_tokens is None


def test_invalid_or_unbounded_backend_numbers_are_dropped():
    telemetry = from_openai_compatible({
        "timings": {
            "prompt_ms": math.inf,
            "predicted_ms": -1,
            "prompt_per_second": 10_000_000,
        }
    })
    assert telemetry is None


def test_metrics_output_uses_only_fixed_content_free_labels():
    registry = MetricsRegistry()
    registry.observe_inference(
        "ollama",
        InferenceTelemetry(
            load_ms=100.0,
            eval_ms=250.0,
            output_tokens_per_second=20.0,
            load_state="cold",
            prompt_tokens=20,
            prompt_cached_tokens=12,
            prompt_uncached_tokens=8,
        ),
    )
    rendered = registry.render().decode("utf-8")
    if registry.enabled:
        assert 'backend="ollama",phase="load"' in rendered
        assert 'backend="ollama",direction="output"' in rendered
        assert 'backend="ollama",state="cold"' in rendered
        assert 'backend="ollama",state="cached"' in rendered
    else:
        assert "metrics disabled" in rendered


def test_model_call_metrics_reduce_models_to_fixed_route_labels():
    registry = MetricsRegistry()
    registry.observe_model_call(
        cloud=False, result="ok", elapsed_seconds=1.25,
    )
    registry.observe_model_call(
        cloud=True, result="arbitrary-provider-error", elapsed_seconds=-5,
    )
    rendered = registry.render().decode("utf-8")
    if registry.enabled:
        assert 'tier="local",result="ok"' in rendered
        assert 'tier="cloud",result="error"' in rendered
        assert "private-model-name" not in rendered
    else:
        assert "metrics disabled" in rendered
