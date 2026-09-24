"""Inference planning for models outside the pinned 1.5b/3b/7b catalog."""

import pytest

from sonder_runtime.application.training.hardware_planning import PlanOptions, build_plan
from system_profile import HardwareProfile


def host(vram, ram):
    return HardwareProfile(
        os_name="Linux", architecture="x86_64",
        system_ram_total_gb=ram, system_ram_available_gb=ram,
        gpu_vendor="nvidia" if vram else "none", gpu_name="mock" if vram else "",
        cuda_available=bool(vram), rocm_available=False,
        vram_total_gb=vram, vram_free_gb=vram,
        compute_capability="8.9" if vram else "",
        cpu_offload_supported=bool(vram),
    )


def options(size, tag, params, **extra):
    return PlanOptions(model=size, requested_model=tag, parameter_billions=params, **extra)


def test_large_requested_model_is_planned_by_its_own_size():
    plan = build_plan(host(48, 128), options("27b", "qwen3.8:27b-q6", 27.0))

    assert plan.inference.model_size == "27b"
    assert plan.inference.model == "qwen3.8:27b-q6"
    assert plan.inference.cpu_offload is False


def test_large_model_that_only_fits_in_system_ram_is_planned_with_offload():
    plan = build_plan(host(16, 64), options("27b", "qwen3.8:27b", 27.0))

    assert plan.inference.model_size == "27b"
    assert plan.inference.cpu_offload is True


def test_requested_model_that_cannot_fit_falls_back_with_a_reason():
    plan = build_plan(host(8, 16), options("70b", "big:70b", 70.0))

    assert plan.inference.model_size in ("1.5b", "3b", "7b")
    assert any("70b" in item and "cannot preserve" in item for item in plan.inference.rejected)


def test_uncatalogued_request_trains_from_the_pinned_catalog():
    plan = build_plan(host(24, 64), options("27b", "qwen3.8:27b", 27.0))

    assert any("no pinned training base" in item for item in plan.training.rejected)
    if plan.training.enabled:
        assert plan.training.model_size in ("1.5b", "3b", "7b")


def test_size_token_without_explicit_parameter_count_is_parsed():
    plan = build_plan(host(48, 128), PlanOptions(model="14b"))

    assert plan.inference.model_size == "14b"
    assert plan.inference.model == "14b"


def test_catalog_sizes_keep_their_pinned_ollama_tags():
    plan = build_plan(host(24, 64), PlanOptions(model="7b"))

    assert plan.inference.model == "qwen2.5-coder:7b"


def test_malformed_size_is_rejected():
    with pytest.raises(ValueError, match="parameter size"):
        build_plan(host(24, 64), PlanOptions(model="huge"))
