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


@pytest.mark.parametrize("size", ("27b", "13"))
def test_uncatalogued_request_disables_training_instead_of_substituting(size):
    # 24 GB NVIDIA / 64 GB RAM would train the pinned 7b under "auto".
    assert build_plan(host(24, 64), PlanOptions(model="auto")).training.enabled

    plan = build_plan(host(24, 64), PlanOptions(model=size))

    assert plan.training.enabled is False
    assert plan.training.model_size == ""
    assert plan.training.model == ""
    assert any("no pinned training base" in item and "disabled" in item
               for item in plan.training.rejected)
    assert plan.inference.enabled
    assert plan.inference.model_size == ("13b" if size == "13" else size)


def test_uncatalogued_request_is_not_substituted_for_dense_training():
    plan = build_plan(host(80, 256), PlanOptions(model="27b", full_finetune=True))

    assert plan.training.enabled is False
    assert not plan.training.method.startswith("full-parameter")


def test_uncatalogued_start_does_not_launch_training():
    import adaptive_training

    launches = []
    plan = build_plan(host(24, 64), PlanOptions(model="27b"))

    ok, message = adaptive_training.start_training(
        plan, confirmed=True, runner=lambda *args, **kwargs: launches.append(args),
    )

    assert ok is False
    assert launches == []
    assert "no pinned training base" in message


def test_catalog_request_still_trains_its_own_size():
    plan = build_plan(host(24, 64), PlanOptions(model="3b"))

    assert plan.training.enabled
    assert plan.training.model_size == "3b"


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
