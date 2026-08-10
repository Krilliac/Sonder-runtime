"""Pure deterministic route planner (SPEC-5 §12).

One RoutePlanner, one authoritative routing decision.  Pure algorithm:
no network, no SQLite, no Ollama call, no cloud call, no permission
mutation.

Wraps the existing capability_router classification + runtime_policy
tier bindings into the SPEC-5 contract:

    RoutingRequest → RoutePlanner.select() → ModelRoute

The RoutePlanner resolves: lane → base tier → capability classification →
specialist override (if bound) → configured model/provider → immutable
ModelRoute.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .capability_router import (
    classify_task,
    recommend_tier,
    escalation_ladder,
    Route as CapabilityRoute,
    route as capability_route,
)
from ..runtime_policy.rules import (
    BASE_LOCAL_TIERS,
    LOCAL_TIERS,
    ROUTING_LANES,
    bound_tiers,
)


@dataclass(frozen=True)
class RoutingRequest:
    lane: str
    prompt: str
    attachments: tuple = ()
    requested_provider: str | None = None
    has_image: bool = False
    approx_tokens: int = 0
    explicit_task: str | None = None
    allow_oracle: bool = False


@dataclass(frozen=True)
class ModelRoute:
    lane: str
    tier: str
    model: str
    provider: str
    capabilities: frozenset[str]
    memory_mode: str = "full"
    retry_policy: str = "local-bounded"
    task_class: str = "simple"
    confidence: float = 0.0
    ladder: tuple[str, ...] = ()


@dataclass(frozen=True)
class AvailableModels:
    """What models are available in the current runtime policy."""
    tier_models: dict[str, str] = field(default_factory=dict)
    provider: str = "ollama"

    @property
    def available_tiers(self) -> frozenset[str]:
        return frozenset(
            t for t, m in self.tier_models.items() if m
        )


class RoutePlanner:
    """Pure, deterministic route selection — no I/O.

    SPEC-5 §12: route planning performs NO network, NO SQLite,
    NO Ollama call, NO cloud call, NO permission mutation.
    """

    def select(
        self,
        request: RoutingRequest,
        policy: dict,
        available: AvailableModels,
    ) -> ModelRoute:
        if request.lane not in ROUTING_LANES:
            raise ValueError(f"unknown lane {request.lane!r}")

        # 1. Resolve lane to base tier from policy
        routing = policy.get("routing", {})
        lane_tier = routing.get(request.lane, "code")

        # 2. Classify required capabilities
        cap_route = capability_route(
            request.prompt,
            available.available_tiers,
            has_image=request.has_image,
            approx_tokens=request.approx_tokens,
            explicit=request.explicit_task,
            allow_oracle=request.allow_oracle,
        )

        # 3. Is matching specialist bound? Use specialist, else base tier
        tier = cap_route.tier
        if tier not in available.available_tiers:
            tier = lane_tier
        if tier not in available.available_tiers:
            for fallback in BASE_LOCAL_TIERS:
                if fallback in available.available_tiers:
                    tier = fallback
                    break

        # 4. Resolve model from tier
        model = available.tier_models.get(tier, "")
        if not model:
            model = available.tier_models.get(lane_tier, "")

        # 5. Determine capabilities
        capabilities: set[str] = set()
        if cap_route.want_web:
            capabilities.add("web")
        if request.has_image:
            capabilities.add("vision")

        return ModelRoute(
            lane=request.lane,
            tier=tier,
            model=model,
            provider=available.provider,
            capabilities=frozenset(capabilities),
            task_class=cap_route.task,
            confidence=cap_route.confidence,
            ladder=cap_route.ladder,
        )

    @staticmethod
    def from_policy(policy: dict, provider: str = "ollama") -> AvailableModels:
        """Build AvailableModels from a runtime policy dict."""
        models = policy.get("local_models", {})
        return AvailableModels(
            tier_models={t: models.get(t, "") for t in LOCAL_TIERS},
            provider=provider,
        )
