from __future__ import annotations

from sonder_runtime.application.training import hardware_planning
import adaptive_training
from sonder_runtime.platform.system_profile import HardwareProfile


def profile(vram=24, ram=64):
    return HardwareProfile(
        os_name="Windows", architecture="x86_64",
        system_ram_total_gb=ram, system_ram_available_gb=ram,
        gpu_vendor="nvidia", gpu_name="test GPU", cuda_available=True,
        vram_total_gb=vram, vram_free_gb=vram, compute_capability="8.9",
        cpu_offload_supported=True,
    )


def test_legacy_names_delegate_to_application_planning_boundary():
    assert adaptive_training.PlanOptions is hardware_planning.PlanOptions
    assert adaptive_training.Recommendation is hardware_planning.Recommendation
    assert adaptive_training.HardwarePlan is hardware_planning.HardwarePlan
    assert adaptive_training.build_plan.__name__ == "build_plan"
    assert adaptive_training.build_plan.__module__ == "adaptive_training"
    assert adaptive_training._application_build_plan is hardware_planning.build_plan
    assert adaptive_training._application_format_plan is hardware_planning.format_plan


def test_planning_boundary_is_side_effect_free_and_typed():
    options = hardware_planning.PlanOptions(model="3b", gpu_index=2)
    plan = hardware_planning.build_plan(profile(), options)
    assert isinstance(plan, hardware_planning.HardwarePlan)
    assert plan.options == options
    assert plan.training.model_size == "3b"
    assert plan.to_dict()["options"]["gpu_index"] == 2


def test_planning_boundary_fails_closed_for_cpu_offload():
    plan = hardware_planning.build_plan(
        profile(), hardware_planning.PlanOptions(model="3b", allow_cpu_offload=True)
    )
    assert not plan.training.enabled
    assert hardware_planning.TRAINING_CPU_OFFLOAD_REASON in plan.training.rejected
