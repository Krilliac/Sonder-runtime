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

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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
from ..inference_profiles import (
    HardwareCapabilityReport,
    QuantizedModelProfile,
    plan_model_execution,
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
    routing_reason: str = ""


@dataclass(frozen=True)
class AvailableModels:
    """What models are available in the current runtime policy."""
    tier_models: dict[str, str] = field(default_factory=dict)
    provider: str = "ollama"
    tier_providers: dict[str, str] = field(default_factory=dict)
    hardware: HardwareCapabilityReport | None = None
    model_profiles: dict[str, QuantizedModelProfile] = field(default_factory=dict)

    @property
    def available_tiers(self) -> frozenset[str]:
        return frozenset(
            t for t, m in self.tier_models.items() if m
        )

    def provider_for(self, tier: str) -> str:
        return self.tier_providers.get(tier, self.provider)


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

        logger.debug(f"select: lane={request.lane!r}, requested_provider={request.requested_provider!r}, allow_oracle={request.allow_oracle}")

        # 1. Resolve lane to base tier from policy
        routing = policy.get("routing", {})
        lane_tier = routing.get(request.lane, "code")
        logger.debug(f"select: lane_tier={lane_tier!r} from policy routing config")

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
            logger.warning(f"preferred tier {cap_route.tier!r} unavailable for lane={request.lane!r}, degrading to lane_tier={lane_tier!r}")
            logger.info(f"route tier fallback: preferred tier {cap_route.tier!r} unavailable for lane={request.lane!r}, trying lane_tier={lane_tier!r}")
            logger.debug(f"select: capability tier {cap_route.tier!r} unavailable, trying lane_tier {lane_tier!r}")
            tier = lane_tier
        if tier not in available.available_tiers:
            for fallback in BASE_LOCAL_TIERS:
                if fallback in available.available_tiers:
                    logger.warning(f"lane_tier {lane_tier!r} also unavailable for lane={request.lane!r}, degrading to base tier {fallback!r} -- routing running with reduced capability")
                    logger.info(f"route tier fallback: lane_tier {lane_tier!r} also unavailable, degrading to base tier {fallback!r}")
                    logger.debug(f"select: lane_tier {lane_tier!r} also unavailable, falling back to {fallback!r}")
                    tier = fallback
                    break

        # 4. Resolve model from tier
        model = available.tier_models.get(tier, "")
        if not model:
            model = available.tier_models.get(lane_tier, "")
            if model:
                logger.warning(f"no model bound to tier {tier!r}, borrowing model from lane_tier {lane_tier!r}: model={model!r}")
            else:
                logger.critical(f"no model resolvable for lane={request.lane!r}: tier={tier!r} and lane_tier={lane_tier!r} both have no bound model -- all inference on this route will fail unconditionally")
                logger.error(f"no model bound to tier {tier!r} or lane_tier {lane_tier!r} for lane={request.lane!r}, route will carry an empty model and downstream inference will fail")
                logger.warning(f"no model bound to tier {tier!r} or lane_tier {lane_tier!r}, route will have an empty model -- check tier configuration")
            logger.debug(f"select: no model for tier {tier!r}, using lane_tier model {model!r}")

        # 5. Determine capabilities
        capabilities: set[str] = set()
        if cap_route.want_web:
            capabilities.add("web")
        if request.has_image:
            capabilities.add("vision")

        memory_mode = "full"
        routing_reason = "capability route"
        model_profile = available.model_profiles.get(model)
        if available.hardware is not None and model_profile is not None:
            execution = plan_model_execution(available.hardware, model_profile)
            memory_mode = execution.mode
            routing_reason = (
                "memory plan: %s; %s"
                % (execution.mode, "; ".join(execution.warnings) or "measured fit")
            )
            capabilities.add("memory:%s" % execution.mode)

        provider = available.provider_for(tier)
        logger.debug(
            f"select: resolved route lane={request.lane!r} tier={tier!r} model={model!r} "
            f"provider={provider!r} task_class={cap_route.task!r} confidence={cap_route.confidence} "
            f"memory_mode={memory_mode!r}"
        )
        return ModelRoute(
            lane=request.lane,
            tier=tier,
            model=model,
            provider=provider,
            capabilities=frozenset(capabilities),
            memory_mode=memory_mode,
            task_class=cap_route.task,
            confidence=cap_route.confidence,
            ladder=cap_route.ladder,
            routing_reason=routing_reason,
        )

    @staticmethod
    def from_policy(
        policy: dict,
        provider: str = "ollama",
        *,
        tier_providers: dict[str, str] | None = None,
        hardware: HardwareCapabilityReport | None = None,
        model_profiles: dict[str, QuantizedModelProfile] | None = None,
    ) -> AvailableModels:
        """Build AvailableModels from a runtime policy dict."""
        models = policy.get("local_models", {})
        tier_models = {t: models.get(t, "") for t in LOCAL_TIERS}
        configured_tiers = {t: m for t, m in tier_models.items() if m}
        empty_tiers = [t for t in LOCAL_TIERS if t not in configured_tiers]
        if empty_tiers:
            logger.warning(f"tiers with no model configured: {empty_tiers} -- these tiers will require fallback during routing")
        logger.info(f"available models configured: provider={provider!r}, tiers={sorted(configured_tiers)}")
        logger.debug(f"from_policy: provider={provider!r}, tier_models={tier_models}")
        return AvailableModels(
            tier_models=tier_models,
            provider=provider,
            tier_providers=dict(tier_providers or {}),
            hardware=hardware,
            model_profiles=dict(model_profiles or {}),
        )
