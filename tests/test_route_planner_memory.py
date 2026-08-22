from sonder_runtime.domain.inference_profiles import (
    build_hardware_capabilities,
    default_30b_profile,
)
from sonder_runtime.domain.routing.route_planner import (
    AvailableModels,
    RoutePlanner,
    RoutingRequest,
)


def test_route_planner_exposes_measured_memory_mode_without_changing_target():
    hardware = build_hardware_capabilities(
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
    model = default_30b_profile()
    available = AvailableModels(
        tier_models={"code": model.model, "fast": "qwen3:8b", "general": model.model},
        hardware=hardware,
        model_profiles={model.model: model},
    )

    route = RoutePlanner().select(
        RoutingRequest(lane="workbench", prompt="implement this function"),
        {"routing": {"workbench": "code"}},
        available,
    )

    assert route.model == model.model
    assert route.memory_mode == "gpu+ram-hybrid"
    assert "memory:gpu+ram-hybrid" in route.capabilities
    assert "lower throughput" in route.routing_reason


def test_route_planner_without_profile_preserves_full_memory_mode():
    route = RoutePlanner().select(
        RoutingRequest(lane="workbench", prompt="implement this function"),
        {"routing": {"workbench": "code"}},
        AvailableModels(tier_models={"code": "qwen3:8b"}),
    )

    assert route.memory_mode == "full"
    assert route.routing_reason == "capability route"
