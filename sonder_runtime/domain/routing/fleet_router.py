"""Dynamic model routing for fleet agents.

Routes agent tasks to appropriate models based on task complexity,
cost constraints, and latency requirements.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    CAPABLE = "capable"
    MAX = "max"


@dataclass(frozen=True)
class RoutingContext:
    """Signals the fleet router uses to select a model tier."""

    task_description: str
    estimated_tokens: int = 0
    max_latency_ms: int = 0
    cost_sensitivity: float = 0.5
    requires_code: bool = False
    requires_reasoning: bool = False
    parent_tier: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_description, str):
            raise TypeError("task_description must be a string")
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        if self.max_latency_ms < 0:
            raise ValueError("max_latency_ms must be non-negative")
        if not 0.0 <= self.cost_sensitivity <= 1.0:
            raise ValueError("cost_sensitivity must be between 0.0 and 1.0")


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of a fleet routing decision."""

    tier: ModelTier
    reason: str
    estimated_cost_factor: float

    def __post_init__(self) -> None:
        if self.estimated_cost_factor < 0:
            raise ValueError("estimated_cost_factor must be non-negative")


# Cost factors relative to a BALANCED baseline of 1.0.
_TIER_COST_FACTORS: dict[ModelTier, float] = {
    ModelTier.FAST: 0.25,
    ModelTier.BALANCED: 1.0,
    ModelTier.CAPABLE: 3.0,
    ModelTier.MAX: 10.0,
}

# Keywords that suggest code generation tasks.
_CODE_KEYWORDS: tuple[str, ...] = (
    "code", "function", "implement", "refactor", "debug", "compile",
    "traceback", "exception", "unit test", "pytest", "regex",
    "algorithm", "python", "javascript", "typescript", "rust",
    "sql", "api", "class ", "def ", "docker", "deploy",
)

# Keywords that suggest chain-of-thought reasoning tasks.
_REASONING_KEYWORDS: tuple[str, ...] = (
    "reason", "prove", "proof", "theorem", "derive", "step by step",
    "think carefully", "analyze", "trade-off", "tradeoff", "strategy",
    "architecture", "design a system", "plan the", "evaluate",
    "compare and contrast", "why does", "explain the reasoning",
)

# Keywords that suggest simple/fast tasks.
_FAST_KEYWORDS: tuple[str, ...] = (
    "summarize", "list", "format", "convert", "translate",
    "rewrite", "rephrase", "extract", "classify", "label",
    "tag", "sort", "filter", "lookup", "check",
)


class FleetRouter:
    """Route fleet agent tasks to the appropriate model tier.

    Pure logic, no network calls.  The router uses keyword-based
    heuristics and numeric thresholds to select a tier.
    """

    def __init__(self, tier_config: dict[ModelTier, dict] | None = None) -> None:
        self._tier_config = dict(tier_config or {})
        logger.info(
            "FleetRouter initialized: %d tier configs provided",
            len(self._tier_config),
        )

    def route(self, context: RoutingContext) -> RoutingDecision:
        """Determine the best model tier for a fleet agent task."""
        tier = self._select_tier(context)
        cost = _TIER_COST_FACTORS.get(tier, 1.0)
        reason = self._build_reason(context, tier)
        logger.debug(
            "fleet route: tier=%s, cost_factor=%.2f, reason=%r",
            tier.value, cost, reason,
        )
        return RoutingDecision(tier=tier, reason=reason, estimated_cost_factor=cost)

    def suggest_tier(self, task_description: str) -> ModelTier:
        """Simple tier suggestion based only on task description keywords."""
        low = (" " + (task_description or "").lower() + " ")

        reasoning_hits = sum(1 for kw in _REASONING_KEYWORDS if kw in low)
        if reasoning_hits >= 2:
            return ModelTier.CAPABLE

        code_hits = sum(1 for kw in _CODE_KEYWORDS if kw in low)
        if code_hits >= 2:
            return ModelTier.BALANCED

        fast_hits = sum(1 for kw in _FAST_KEYWORDS if kw in low)
        if fast_hits >= 1:
            return ModelTier.FAST

        return ModelTier.BALANCED

    # ------------------------------------------------------------------
    # Internal routing logic
    # ------------------------------------------------------------------

    def _select_tier(self, ctx: RoutingContext) -> ModelTier:
        """Core heuristic: combine all signals into a single tier."""

        # 1. Parent at MAX -> cap child at CAPABLE (cost control).
        parent_cap = ctx.parent_tier.lower() if ctx.parent_tier else ""
        cap_at_capable = parent_cap == ModelTier.MAX.value

        # 2. Explicit reasoning requirement -> CAPABLE.
        if ctx.requires_reasoning:
            tier = ModelTier.CAPABLE
            if cap_at_capable:
                logger.debug(
                    "reasoning requested but parent is MAX, capping at CAPABLE"
                )
            return ModelTier.CAPABLE if cap_at_capable else self._apply_cost_bias(
                tier, ctx.cost_sensitivity,
            )

        # 3. Explicit code requirement -> BALANCED or CAPABLE.
        if ctx.requires_code:
            base = ModelTier.BALANCED
            # Upgrade to CAPABLE for large code tasks when cost allows.
            if ctx.estimated_tokens >= 4000 and ctx.cost_sensitivity < 0.7:
                base = ModelTier.CAPABLE
            if cap_at_capable and base == ModelTier.MAX:
                base = ModelTier.CAPABLE
            return self._apply_cost_bias(base, ctx.cost_sensitivity)

        # 4. Short tasks with no special requirements -> FAST.
        if ctx.estimated_tokens > 0 and ctx.estimated_tokens < 1000:
            tier = ModelTier.FAST
            if cap_at_capable:
                return tier
            return self._apply_cost_bias(tier, ctx.cost_sensitivity)

        # 5. Strict latency constraint -> prefer FAST.
        if ctx.max_latency_ms > 0 and ctx.max_latency_ms < 500:
            return ModelTier.FAST

        # 6. Keyword-based suggestion from the task description.
        suggested = self.suggest_tier(ctx.task_description)

        # 7. Apply parent cap.
        if cap_at_capable and suggested == ModelTier.MAX:
            suggested = ModelTier.CAPABLE

        # 8. Apply cost bias.
        return self._apply_cost_bias(suggested, ctx.cost_sensitivity)

    @staticmethod
    def _apply_cost_bias(tier: ModelTier, cost_sensitivity: float) -> ModelTier:
        """Shift the tier down when cost sensitivity is high."""
        if cost_sensitivity >= 0.9:
            # Very high cost sensitivity -> always FAST.
            return ModelTier.FAST
        if cost_sensitivity >= 0.8:
            # High cost sensitivity -> at most BALANCED.
            if tier in (ModelTier.CAPABLE, ModelTier.MAX):
                return ModelTier.BALANCED
        return tier

    @staticmethod
    def _build_reason(ctx: RoutingContext, tier: ModelTier) -> str:
        """Produce a human-readable explanation of the routing decision."""
        parts: list[str] = []

        if ctx.requires_reasoning:
            parts.append("reasoning required")
        if ctx.requires_code:
            parts.append("code generation")
        if ctx.estimated_tokens > 0 and ctx.estimated_tokens < 1000:
            parts.append("short task")
        if ctx.max_latency_ms > 0 and ctx.max_latency_ms < 500:
            parts.append("strict latency")
        if ctx.cost_sensitivity >= 0.8:
            parts.append("cost sensitive")
        if ctx.parent_tier.lower() == ModelTier.MAX.value:
            parts.append("parent at max, child capped")

        if not parts:
            parts.append("default heuristic")

        return f"{tier.value}: {'; '.join(parts)}"
