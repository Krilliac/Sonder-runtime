from __future__ import annotations

import pytest

from sonder_runtime.domain.inference_profiles import (
    DEFAULT_30B_MODEL,
    build_hardware_capabilities,
    default_30b_profile,
    plan_model_execution,
    quantized_model_profile,
)


def test_capability_report_keeps_driver_detection_separate_from_backend_ready():
    report = build_hardware_capabilities(
        {
            "platform": "Windows",
            "architecture": "AMD64",
            "cpu_count": 16,
            "total_ram_gb": 64,
            "gpu_vendor": "nvidia",
            "gpu_name": "RTX 5070 Ti",
            "vram_gb": 16.0,
            "vram_free_gb": 14.0,
            "vram_availability_live": True,
            "cuda_available": True,
            "compute_capability": "12.0",
        },
        backend_readiness={"cuda": None, "ollama": True},
    )

    assert report.tensor_cores is True
    assert report.vram_is_live is True
    assert report.safe_gpu_offload is False
    assert dict(report.backend_readiness) == {"cuda": "unknown", "ollama": "ready"}
    assert "cuda" in report.backend_candidates


def test_30b_q4_profile_includes_weights_kv_overhead_and_safety():
    profile = default_30b_profile()

    assert profile.model == DEFAULT_30B_MODEL
    assert profile.quantization == "Q4_K_M"
    assert profile.weight_gb == 18.6
    assert profile.kv_cache_gb == 0.5
    assert profile.required_gb == 22.1
    assert profile.to_dict()["required_gb"] == 22.1


def test_30b_on_16gb_gpu_is_explicitly_hybrid_not_gpu_resident():
    capabilities = build_hardware_capabilities(
        {
            "gpu_vendor": "nvidia",
            "vram_gb": 16.0,
            "vram_free_gb": 14.0,
            "vram_availability_live": True,
            "cuda_available": True,
            "total_ram_gb": 64.0,
        },
        backend_readiness={"ollama": True},
    )
    plan = plan_model_execution(capabilities, default_30b_profile())

    assert plan.mode == "gpu+ram-hybrid"
    assert plan.safe_gpu_offload is False
    assert plan.gpu_layers == "auto"
    assert plan.fallback_model == "qwen3:14b"
    assert any("free VRAM" in warning for warning in plan.warnings)


def test_gpu_resident_requires_both_fit_and_measured_backend_readiness():
    capabilities = build_hardware_capabilities(
        {
            "gpu_vendor": "nvidia",
            "vram_gb": 48.0,
            "vram_free_gb": 40.0,
            "vram_availability_live": True,
            "cuda_available": True,
        },
        backend_readiness={"ollama": True},
    )
    profile = quantized_model_profile(total_params_b=14, active_params_b=14)

    assert plan_model_execution(capabilities, profile).mode == "gpu-resident"


def test_hybrid_requires_combined_reported_memory_to_cover_model_envelope():
    capabilities = build_hardware_capabilities(
        {
            "gpu_vendor": "nvidia",
            "vram_gb": 16.0,
            "vram_free_gb": 10.0,
            "vram_availability_live": True,
            "cuda_available": True,
            "total_ram_gb": 1.0,
        },
        backend_readiness={"ollama": True},
    )

    plan = plan_model_execution(capabilities, default_30b_profile())

    assert plan.mode == "cpu-fallback"
    assert any("below the model envelope" in warning for warning in plan.warnings)


def test_unknown_quantization_and_invalid_parameter_counts_fail_closed():
    with pytest.raises(ValueError):
        quantized_model_profile(quantization="made-up")
    with pytest.raises(ValueError):
        quantized_model_profile(total_params_b=3, active_params_b=4)
