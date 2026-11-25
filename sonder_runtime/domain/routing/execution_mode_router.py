"""Pure deterministic difficulty-to-execution-mode router.

Decides *how* to execute a turn -- single model, parallel multi-model ensemble
(e.g. local GPU ``code`` + Node1 ``reasoning``), bounded local fan-out, or
autonomous ``/agent`` -- from prompt difficulty signals, bound local capacity,
and explicit operator policy.

Pure domain module: no I/O, no environment reads, no network, no SQLite, and
no permission mutations.  The application service loads operator settings and
available tier bindings and passes them into :func:`plan_execution_mode`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

ExecutionMode = Literal["single", "ensemble", "fanout", "agent"]
DifficultyBand = Literal["quick", "standard", "hard", "multi_scope"]
ExecutionModeRequest = Literal["off", "auto", "single", "ensemble", "fanout", "agent"]
EnsembleMode = Literal["prose", "code"]

VALID_EXECUTION_MODES: frozenset[str] = frozenset(
    {"off", "auto", "single", "ensemble", "fanout", "agent"}
)

_HARD_REASONING_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "explicit_hard_cue",
        re.compile(
            r"\b(think\s+hard|think\s+carefully|be\s+rigorous|reason\s+through|"
            r"step\s+by\s+step|deep\s+dive|second\s+opinion|double[- ]check)\b",
            re.I,
        ),
        0.38,
    ),
    (
        "architecture_tradeoff",
        re.compile(
            r"\b(trade[- ]?offs?|architect(?:ure|ural)?|design\s+a\s+(?:system|protocol|engine|pipeline)|"
            r"system\s+design|invariants?|failure\s+modes?|distributed|consensus)\b",
            re.I,
        ),
        0.34,
    ),
    (
        "concurrency_memory_systems",
        re.compile(
            r"\b(race\s+condition|deadlock|lock[- ]?free|memory\s+leak|use[- ]after[- ]free|"
            r"data\s+race|memory\s+order|acquire/release|aba\s+problem|ubsan|tsan|asan)\b",
            re.I,
        ),
        0.36,
    ),
    (
        "security_analysis",
        re.compile(
            r"\b(threat\s+model|security\s+audit|attack\s+surface|privilege\s+escalation|"
            r"ssrf|path\s+traversal|injection|cryptograph\w*|timing\s+attack)\b",
            re.I,
        ),
        0.34,
    ),
    (
        "formal_math_proof",
        re.compile(
            r"\b(prove\b|proof\b|theorem\b|derive\b|asymptotic|big[- ]o\b|time\s+complexity|"
            r"correctness\s+proof|induction|counterexample)\b",
            re.I,
        ),
        0.34,
    ),
    (
        "root_cause_debug",
        re.compile(
            r"\b(root\s+cause|why\s+(?:does|is|would|did)\b|intermittent|flaky|"
            r"heisenbug|subtle\s+bug|diagnos\w*|bottleneck|profil(?:e|ing))\b",
            re.I,
        ),
        0.26,
    ),
    (
        "comparative_analysis",
        re.compile(
            r"\b(compare\s+and\s+contrast|versus\b|\bvs\.?\b|pros\s+and\s+cons|"
            r"evaluate\s+(?:options|approaches|alternatives))\b",
            re.I,
        ),
        0.24,
    ),
)

_MULTI_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "multi_angle_parallel",
        re.compile(
            r"\b(in\s+parallel|from\s+multiple\s+angles|multiple\s+perspectives|"
            r"fan[- ]?out|across\s+(?:all|multiple)\s+(?:modules|services|files|layers|repos))\b",
            re.I,
        ),
        0.46,
    ),
    (
        "multi_target_audit",
        re.compile(
            r"\b(audit|review|inspect|compare|benchmark)\b.{0,80}\b(?:and|&)\b.{0,80}\b(?:and|&)\b",
            re.I | re.S,
        ),
        0.40,
    ),
    (
        "end_to_end_multi_layer",
        re.compile(
            r"\b(end[- ]to[- ]end|full[- ]stack|frontend\s+and\s+backend|"
            r"across\s+the\s+(?:codebase|repo|repository|stack)|entire\s+repo)\b",
            re.I,
        ),
        0.44,
    ),
)

_MUTATING_AGENT_PATTERN = re.compile(
    r"\b(implement|refactor|migrate|rewrite|fix\s+all|update\s+all)\b.{0,120}"
    r"\b(tests?|build|ci|verify|commit|pull\s+request|pr\b|frontend\s+and\s+backend|"
    r"across\s+(?:multiple\s+)?files|multiple\s+modules)\b",
    re.I | re.S,
)

_NUMBERED_LIST_PATTERN = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+\S", re.M)

_CODE_INTENT_PATTERN = re.compile(
    r"\b(write\s+(?:a|an|the)?\s*(?:python|c\+\+|cpp|rust|dart|ts|typescript|go|sql|function|class|script|module)|"
    r"implement\s+(?:a|an|the)?\b|code\s+for\b|source\s+file\b|compilable\b|refactor\s+this)\b",
    re.I,
)

_QUICK_GREETING_PATTERN = re.compile(
    r"^\s*(?:hi|hello|hey|yo|thanks|thank\s+you|ok|okay|cool|nice|ping|test|status|"
    r"what\s+time|who\s+are\s+you)\b",
    re.I,
)

_STANDARD_WORK_PATTERN = re.compile(
    r"\b(write|implement|fix|refactor|convert|translate|summarize|explain|format|"
    r"generate|create|add|remove|edit|debug|regex|sql|json|yaml|script|function|class)\b",
    re.I,
)


@dataclass(frozen=True)
class ExecutionModePolicy:
    """Operator policy governing automatic execution-mode routing."""

    mode: ExecutionModeRequest = "off"
    allow_auto_ensemble: bool = True
    allow_auto_fanout: bool = True
    allow_auto_agent: bool = False
    ensemble_tiers: tuple[str, ...] = ("code", "reasoning")
    fanout_scope: str = "local"


@dataclass(frozen=True)
class ExecutionContext:
    """Pure snapshot of the request and locally bound capacity."""

    prompt: str
    available_tiers: frozenset[str]
    distinct_ensemble_models: int = 0
    distinct_local_models: int = 0
    developer_authorized: bool = False
    has_image: bool = False
    structured_output: bool = False
    explicit_model_pin: bool = False
    requested_mode: str = ""


@dataclass(frozen=True)
class ExecutionDecision:
    """Authoritative execution-mode decision returned to callers and receipts."""

    mode: ExecutionMode
    band: DifficultyBand
    difficulty_score: float
    recommended_tiers: tuple[str, ...]
    ensemble_mode: EnsembleMode
    fanout_scope: str
    reason: str
    signals: tuple[str, ...]
    auto_selected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "band": self.band,
            "difficulty_score": round(self.difficulty_score, 3),
            "recommended_tiers": list(self.recommended_tiers),
            "ensemble_mode": self.ensemble_mode,
            "fanout_scope": self.fanout_scope,
            "reason": self.reason,
            "signals": list(self.signals),
            "auto_selected": self.auto_selected,
        }


def check_execution_mode(raw: Any) -> ExecutionModeRequest | None:
    """Validate an optional execution_mode value or return ``None`` if invalid."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized not in VALID_EXECUTION_MODES:
        return None
    if normalized == "off":
        return "off"
    if normalized == "auto":
        return "auto"
    if normalized == "single":
        return "single"
    if normalized == "ensemble":
        return "ensemble"
    if normalized == "fanout":
        return "fanout"
    return "agent"


def score_difficulty(
    prompt: str,
) -> tuple[DifficultyBand, float, tuple[str, ...], EnsembleMode, bool]:
    """Score prompt difficulty deterministically.

    Returns ``(band, score_0_to_1, signals, ensemble_mode, wants_mutating_agent)``.
    """
    text = (prompt or "").strip()
    if not text:
        return "quick", 0.0, ("empty_prompt",), "prose", False

    score = 0.0
    signals: list[str] = []

    hard_hits = 0
    for name, pattern, weight in _HARD_REASONING_PATTERNS:
        if pattern.search(text):
            hard_hits += 1
            score += weight
            signals.append(name)

    multi_hits = 0
    for name, pattern, weight in _MULTI_SCOPE_PATTERNS:
        if pattern.search(text):
            multi_hits += 1
            score += weight
            signals.append(name)

    wants_mutating_agent = bool(_MUTATING_AGENT_PATTERN.search(text))
    if wants_mutating_agent:
        score += 0.42
        signals.append("multi_file_mutating_work")

    bullet_count = len(_NUMBERED_LIST_PATTERN.findall(text))
    if bullet_count >= 3:
        score += 0.34
        signals.append(f"structured_subitems:{bullet_count}")
    elif bullet_count == 2:
        score += 0.16
        signals.append("structured_subitems:2")

    char_len = len(text)
    if char_len >= 600:
        score += 0.22
        signals.append("long_prompt")
    elif char_len >= 240:
        score += 0.12
        signals.append("medium_prompt")

    question_marks = text.count("?")
    if question_marks >= 3:
        score += 0.18
        signals.append(f"multi_question:{question_marks}")

    if score == 0.0:
        if char_len <= 90 and (
            _QUICK_GREETING_PATTERN.search(text)
            or not _STANDARD_WORK_PATTERN.search(text)
        ):
            score = 0.12
            signals.append("short_simple_prompt")
        else:
            score = 0.35
            signals.append("standard_task")
    elif hard_hits == 0 and multi_hits == 0 and not wants_mutating_agent:
        # Pure length/bullets without hard or multi-scope cues stays in standard
        # unless heavily multi-part.
        if score < 0.30:
            score = 0.35

    clamped = max(0.0, min(1.0, round(score, 3)))

    has_multi_scope_signal = (
        multi_hits >= 1
        or wants_mutating_agent
        or bullet_count >= 3
        or question_marks >= 3
    )

    if has_multi_scope_signal and (
        (multi_hits >= 1 and (hard_hits >= 1 or bullet_count >= 2 or char_len >= 120))
        or (wants_mutating_agent and (multi_hits >= 1 or hard_hits >= 1 or bullet_count >= 2 or char_len >= 80))
        or (bullet_count >= 3 and hard_hits >= 1)
        or (question_marks >= 3 and hard_hits >= 1)
        or clamped >= 0.85
    ):
        band: DifficultyBand = "multi_scope"
        clamped = max(clamped, 0.85)
    elif clamped >= 0.60 or hard_hits >= 2:
        band = "hard"
        clamped = max(clamped, 0.60)
    elif clamped >= 0.25:
        band = "standard"
    else:
        band = "quick"

    ensemble_mode: EnsembleMode = (
        "code" if _CODE_INTENT_PATTERN.search(text) else "prose"
    )
    return band, clamped, tuple(signals), ensemble_mode, wants_mutating_agent


def _pick_single_tier(available: frozenset[str], preferred: tuple[str, ...]) -> tuple[str, ...]:
    for tier in preferred:
        if tier in available:
            return (tier,)
    if available:
        return (sorted(available)[0],)
    return ("code",)


def _bound_ensemble_tiers(
    available: frozenset[str], configured: tuple[str, ...]
) -> tuple[str, ...]:
    selected = tuple(t for t in configured if t in available)
    if len(selected) >= 2:
        return selected
    fallback_order = ("code", "reasoning", "general", "creative", "fast")
    merged = list(selected)
    for tier in fallback_order:
        if tier in available and tier not in merged:
            merged.append(tier)
        if len(merged) >= 2:
            break
    return tuple(merged) if merged else _pick_single_tier(available, ("code", "general"))


def plan_execution_mode(
    ctx: ExecutionContext,
    policy: ExecutionModePolicy,
) -> ExecutionDecision:
    """Compute the execution mode for a single request under *policy*."""
    band, score, signals, ensemble_mode, wants_mutating_agent = score_difficulty(
        ctx.prompt
    )
    available = frozenset(t.strip().lower() for t in ctx.available_tiers if t)
    ens_tiers = _bound_ensemble_tiers(available, policy.ensemble_tiers)
    fanout_scope = (policy.fanout_scope or "local").strip().lower() or "local"

    req_mode = check_execution_mode(ctx.requested_mode) if ctx.requested_mode else None
    effective_mode: ExecutionModeRequest = req_mode if req_mode is not None else policy.mode

    # Hard bypasses: never override structured output, images, or pinned models.
    if ctx.structured_output:
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="structured response_format requires single-model execution",
            signals=signals,
            auto_selected=False,
        )
    if ctx.has_image:
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("vision", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="multimodal image input routes to single vision model",
            signals=signals,
            auto_selected=False,
        )
    if ctx.explicit_model_pin and req_mode not in ("ensemble", "fanout", "agent"):
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="caller pinned an explicit model/tier",
            signals=signals,
            auto_selected=False,
        )

    if effective_mode in ("off", "single"):
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=(
                "explicit single execution mode requested"
                if req_mode == "single"
                else "auto execution-mode router disabled"
            ),
            signals=signals,
            auto_selected=False,
        )

    # Explicit non-single caller requests (auth is also enforced by HTTP facade).
    if effective_mode == "ensemble":
        if ctx.developer_authorized and ctx.distinct_ensemble_models >= 2:
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="explicit ensemble execution mode requested",
                signals=signals,
                auto_selected=False,
            )
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("reasoning", "code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=(
                "ensemble unavailable: developer authorization required"
                if not ctx.developer_authorized
                else "ensemble degraded to single: fewer than 2 distinct local models"
            ),
            signals=signals,
            auto_selected=False,
        )

    if effective_mode == "fanout":
        if ctx.developer_authorized and ctx.distinct_local_models >= 2:
            return ExecutionDecision(
                mode="fanout",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="explicit fanout execution mode requested",
                signals=signals,
                auto_selected=False,
            )
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("reasoning", "code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=(
                "fanout unavailable: developer authorization required"
                if not ctx.developer_authorized
                else "fanout degraded to single: fewer than 2 distinct local models"
            ),
            signals=signals,
            auto_selected=False,
        )

    if effective_mode == "agent":
        if ctx.developer_authorized:
            return ExecutionDecision(
                mode="agent",
                band=band,
                difficulty_score=score,
                recommended_tiers=_pick_single_tier(available, ("code", "reasoning", "general")),
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="explicit agent execution mode requested",
                signals=signals,
                auto_selected=False,
            )
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="agent mode unavailable: developer authorization required",
            signals=signals,
            auto_selected=False,
        )

    # Automatic difficulty-based routing (effective_mode == "auto").
    if band == "quick":
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("fast", "general", "code")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="quick low-complexity prompt routed to fast single tier",
            signals=signals,
            auto_selected=True,
        )

    if band == "standard":
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="standard single-scope prompt routed to primary local tier",
            signals=signals,
            auto_selected=True,
        )

    if band == "hard":
        if (
            ctx.developer_authorized
            and policy.allow_auto_ensemble
            and ctx.distinct_ensemble_models >= 2
        ):
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="hard reasoning prompt auto-routed to parallel multi-model ensemble",
                signals=signals,
                auto_selected=True,
            )
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("reasoning", "code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason=(
                "hard prompt degraded to single strongest tier: auto-ensemble unavailable"
                if ctx.distinct_ensemble_models < 2
                else "hard prompt routed to single strongest tier"
            ),
            signals=signals,
            auto_selected=True,
        )

    # band == "multi_scope"
    if wants_mutating_agent:
        if ctx.developer_authorized and policy.allow_auto_agent:
            return ExecutionDecision(
                mode="agent",
                band=band,
                difficulty_score=score,
                recommended_tiers=_pick_single_tier(available, ("code", "reasoning", "general")),
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="multi-scope implementation/verification task auto-routed to /agent",
                signals=signals,
                auto_selected=True,
            )
        if (
            ctx.developer_authorized
            and policy.allow_auto_ensemble
            and ctx.distinct_ensemble_models >= 2
        ):
            return ExecutionDecision(
                mode="ensemble",
                band=band,
                difficulty_score=score,
                recommended_tiers=ens_tiers,
                ensemble_mode=ensemble_mode,
                fanout_scope=fanout_scope,
                reason="multi-scope task stepped down to ensemble (auto-agent disabled)",
                signals=signals,
                auto_selected=True,
            )
        return ExecutionDecision(
            mode="single",
            band=band,
            difficulty_score=score,
            recommended_tiers=_pick_single_tier(available, ("reasoning", "code", "general")),
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="multi-scope task kept on single tier (auto-agent disabled)",
            signals=signals,
            auto_selected=True,
        )

    # Read-only multi-scope prompt: prefer bounded local fanout, then ensemble.
    if (
        ctx.developer_authorized
        and policy.allow_auto_fanout
        and ctx.distinct_local_models >= 2
    ):
        return ExecutionDecision(
            mode="fanout",
            band=band,
            difficulty_score=score,
            recommended_tiers=ens_tiers,
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="multi-scope comparative/audit prompt auto-routed to bounded local fanout",
            signals=signals,
            auto_selected=True,
        )
    if (
        ctx.developer_authorized
        and policy.allow_auto_ensemble
        and ctx.distinct_ensemble_models >= 2
    ):
        return ExecutionDecision(
            mode="ensemble",
            band=band,
            difficulty_score=score,
            recommended_tiers=ens_tiers,
            ensemble_mode=ensemble_mode,
            fanout_scope=fanout_scope,
            reason="multi-scope prompt auto-routed to parallel multi-model ensemble",
            signals=signals,
            auto_selected=True,
        )
    return ExecutionDecision(
        mode="single",
        band=band,
        difficulty_score=score,
        recommended_tiers=_pick_single_tier(available, ("reasoning", "code", "general")),
        ensemble_mode=ensemble_mode,
        fanout_scope=fanout_scope,
        reason="multi-scope prompt degraded to single strongest tier",
        signals=signals,
        auto_selected=True,
    )


__all__ = [
    "DifficultyBand",
    "EnsembleMode",
    "ExecutionContext",
    "ExecutionDecision",
    "ExecutionMode",
    "ExecutionModePolicy",
    "ExecutionModeRequest",
    "VALID_EXECUTION_MODES",
    "check_execution_mode",
    "plan_execution_mode",
    "score_difficulty",
]
