"""Pure routing and prompt-framing contracts for bound thinking-tier ensembling.

When an operator binds a local thinking tier (``thinking`` or ``reasoning``, for
example a large reasoning model hosted on Node1 or a secondary worker), this
module decides:

1. Whether a ``code`` or ``security`` lane automatically ensembles with that
   bound thinking tier (deduplicated by resolved model, local-only by default).
2. Whether main-workstation Sonder (``sonder`` / ``general`` / ``code`` /
   ``security``) should delegate its pre-turn ``Thinking`` phase to the bound
   thinking tier and feed the resulting advisory reasoning brief into the fast
   primary model.

Pure stdlib; no I/O, no network, no server imports.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

THINKING_TIER_CANDIDATES: tuple[str, ...] = ("thinking", "reasoning")
AUTO_ENSEMBLE_LANES: frozenset[str] = frozenset({"code", "security"})
MAIN_THINKING_LANES: frozenset[str] = frozenset({"", "sonder", "local", "code", "general", "security"})
THINKING_ENSEMBLE_ENV = "SONDER_THINKING_ENSEMBLE"
THINKING_DELEGATION_ENV = "SONDER_THINKING_DELEGATION"

_FALSE_TOKENS = frozenset({"0", "false", "no", "off", "disabled"})
_SUBSTANTIVE_CUES = re.compile(
    r"\b(?:fix|implement|refactor|design|architect|debug|audit|security|vulnerability|"
    r"exploit|threat|review|analy[sz]e|explain|why|how|compare|trade[- ]?off|plan|"
    r"build|test|optimi[sz]e|patch|migrate|investigate|verify|prove|derive|code|"
    r"function|class|error|traceback|exception|bug|api|sql|regex|protocol|auth)\b|```",
    re.IGNORECASE,
)
_INLINE_THINK_BLOCK_RE = re.compile(
    r"<(?:think|thinking|thought|\|thought\|)(?:\s+[^>]*)?>(.*?)</(?:think|thinking|thought|\|thought\|)>",
    re.DOTALL | re.IGNORECASE,
)
_MANIFEST_HEADER_PREFIX = "Tools in this turn ("


def is_cloud_model(model: object) -> bool:
    """Return True if *model* names a cloud/hosted Ollama suffix."""
    text = str(model or "").strip().lower()
    return text.endswith(":cloud") or "-cloud" in text


def thinking_ensemble_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True unless explicitly disabled via ``SONDER_THINKING_ENSEMBLE``."""
    env = environ or {}
    for key in (THINKING_ENSEMBLE_ENV, THINKING_DELEGATION_ENV):
        raw = str(env.get(key, "") or "").strip().lower()
        if raw in _FALSE_TOKENS:
            return False
    return True


def normalize_tier_alias(tier: str, tiers: Mapping[str, str]) -> str:
    """Resolve ``thinking`` alias to ``reasoning`` when ``thinking`` is not separately bound."""
    cleaned = str(tier or "").strip().lower()
    if (
        cleaned == "thinking"
        and not str(tiers.get("thinking") or "").strip()
        and str(tiers.get("reasoning") or "").strip()
    ):
        return "reasoning"
    return cleaned


def resolve_thinking_tier(
    tiers: Mapping[str, str],
    *,
    primary_tier: str = "",
    primary_model: str = "",
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> tuple[str, str] | None:
    """Return ``(tier_name, model_name)`` for the bound distinct local thinking tier, or ``None``."""
    primary_tier_lc = str(primary_tier or "").strip().lower()
    primary_model_cf = str(primary_model or "").strip().casefold()
    if not primary_model_cf and primary_tier_lc:
        primary_model_cf = str(tiers.get(primary_tier_lc) or "").strip().casefold()

    for candidate in THINKING_TIER_CANDIDATES:
        if candidate == primary_tier_lc:
            continue
        model = str(tiers.get(candidate) or "").strip()
        if not model:
            continue
        if is_cloud_model(model):
            continue
        if is_cloud_fn is not None and is_cloud_fn(candidate, model):
            continue
        if primary_model_cf and model.casefold() == primary_model_cf:
            continue
        return candidate, model
    return None


def expand_lane_ensemble_tiers(
    requested: Sequence[str],
    tiers: Mapping[str, str],
    *,
    enabled: bool = True,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> list[str]:
    """Automatically pair a single ``code`` or ``security`` lane with the bound thinking tier.

    When ``requested`` names multiple tiers or a non-code/security tier, returns
    the normalized list unchanged. Never silently adds a cloud model.
    """
    normalized = [normalize_tier_alias(t, tiers) for t in requested if str(t or "").strip()]
    if not enabled or len(normalized) != 1:
        return normalized
    primary = normalized[0]
    if primary not in AUTO_ENSEMBLE_LANES:
        return normalized
    resolved = resolve_thinking_tier(
        tiers,
        primary_tier=primary,
        primary_model=str(tiers.get(primary) or ""),
        is_cloud_fn=is_cloud_fn,
    )
    if resolved is None:
        return normalized
    thinking_tier, _thinking_model = resolved
    if thinking_tier not in normalized:
        return [primary, thinking_tier]
    return normalized


def should_delegate_thinking(
    tier: str,
    prompt: str,
    tiers: Mapping[str, str],
    *,
    primary_model: str = "",
    enabled: bool = True,
    cloud: bool = False,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> tuple[str, str] | None:
    """Decide whether main-workstation Sonder should delegate Thinking to the bound thinking tier."""
    if not enabled or cloud:
        return None
    text = str(prompt or "").strip()
    if not text or text.startswith("/"):
        return None
    lane = str(tier or "").strip().lower()
    if lane not in MAIN_THINKING_LANES:
        return None
    if lane in AUTO_ENSEMBLE_LANES:
        if len(text) < 12:
            return None
    else:
        if len(text) < 24 and not _SUBSTANTIVE_CUES.search(text):
            return None
    return resolve_thinking_tier(
        tiers,
        primary_tier=lane,
        primary_model=primary_model,
        is_cloud_fn=is_cloud_fn,
    )


def build_thinking_prompt(
    prompt: str,
    *,
    lane: str = "",
    history_tail: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build a concise, bounded deliberation request for the bound thinking tier."""
    lane_lc = str(lane or "").strip().lower()
    if lane_lc == "security":
        focus = (
            "Focus on threat modeling, trust boundaries, auth/permission invariants, "
            "bypass/injection/path-traversal vectors, and concrete verification checks."
        )
    elif lane_lc in {"code", "coder"}:
        focus = (
            "Focus on architecture, invariants, subtle edge cases, API/type contracts, "
            "potential regressions, and concrete verification steps."
        )
    else:
        focus = (
            "Focus on decomposing the request, identifying hidden constraints or pitfalls, "
            "noting which tools/facts to verify, and outlining a clear execution plan."
        )
    history_lines: list[str] = []
    for item in list(history_tail or ())[-4:]:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = " ".join(str(item.get("content") or "").split()).strip()
        if role in {"user", "assistant"} and content:
            history_lines.append(f"{role}: {content[:240]}")
    context_block = ""
    if history_lines:
        context_block = "Recent conversation context:\n" + "\n".join(history_lines) + "\n\n"
    bounded_prompt = str(prompt or "").strip()[:6000]
    return (
        "You are acting as the internal Thinking tier for Sonder's primary local model. "
        "Produce a concise, high-signal bulleted reasoning brief (at most 10 bullets) "
        "to guide the primary model's answer or tool actions. "
        f"{focus} "
        "Do NOT write the final user-facing essay and NEVER claim you executed tools or read files.\n\n"
        f"{context_block}"
        f"User task:\n{bounded_prompt}"
    )


def extract_thinking_and_brief(raw_message: Mapping[str, Any] | str) -> tuple[str, str]:
    """Return ``(thinking_text, brief_text)`` from a raw Ollama/OpenAI message or string."""
    if isinstance(raw_message, Mapping):
        explicit_thinking = str(raw_message.get("thinking") or "").strip()
        raw_content = str(raw_message.get("content") or "").strip()
    else:
        explicit_thinking = ""
        raw_content = str(raw_message or "").strip()

    inline_parts = [m.group(1).strip() for m in _INLINE_THINK_BLOCK_RE.finditer(raw_content) if m.group(1).strip()]
    stripped_content = _INLINE_THINK_BLOCK_RE.sub("", raw_content).strip()

    thinking_parts = [part for part in (explicit_thinking, *inline_parts) if part]
    thinking_text = "\n".join(thinking_parts).strip()
    brief_text = stripped_content or thinking_text
    if not thinking_text and brief_text:
        thinking_text = brief_text
    return thinking_text, brief_text


def format_thinking_brief(
    brief: str,
    *,
    thinking_tier: str,
    thinking_model: str,
    lane: str = "",
    max_chars: int = 2400,
) -> str:
    """Wrap the thinking tier's reasoning brief as an advisory host scaffold."""
    cleaned = str(brief or "").strip()
    if not cleaned:
        return ""
    safe_lines = [
        line.replace(_MANIFEST_HEADER_PREFIX, "Tools note (")
        for line in cleaned.splitlines()
    ]
    body = "\n".join(safe_lines).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n... (thinking brief truncated)"
    lane_note = f" for {lane} lane" if lane else ""
    return (
        f"INTERNAL THINKING BRIEF (delegated to {thinking_tier} tier `{thinking_model}`{lane_note}; "
        "advisory deliberation only — verify facts, files, and tests with tools before claiming results):\n"
        f"{body}\n"
        "END INTERNAL THINKING BRIEF"
    )


def thinking_ensemble_status(
    tiers: Mapping[str, str],
    *,
    enabled: bool = True,
    is_cloud_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Return a content-free status snapshot for ``runtime_policy_data`` and diagnostics."""
    resolved = resolve_thinking_tier(tiers, is_cloud_fn=is_cloud_fn)
    bound_tier = resolved[0] if resolved is not None else ""
    bound_model = resolved[1] if resolved is not None else ""
    code_pair = resolve_thinking_tier(tiers, primary_tier="code", is_cloud_fn=is_cloud_fn)
    sec_pair = resolve_thinking_tier(tiers, primary_tier="security", is_cloud_fn=is_cloud_fn)
    return {
        "enabled": bool(enabled),
        "bound_tier": bound_tier,
        "bound_model": bound_model,
        "active": bool(enabled and resolved is not None),
        "auto_ensemble_lanes": sorted(AUTO_ENSEMBLE_LANES),
        "code_ensembles_with": code_pair[0] if (enabled and code_pair is not None) else "",
        "security_ensembles_with": sec_pair[0] if (enabled and sec_pair is not None) else "",
    }


__all__ = [
    "AUTO_ENSEMBLE_LANES",
    "MAIN_THINKING_LANES",
    "THINKING_DELEGATION_ENV",
    "THINKING_ENSEMBLE_ENV",
    "THINKING_TIER_CANDIDATES",
    "build_thinking_prompt",
    "expand_lane_ensemble_tiers",
    "extract_thinking_and_brief",
    "format_thinking_brief",
    "is_cloud_model",
    "normalize_tier_alias",
    "resolve_thinking_tier",
    "should_delegate_thinking",
    "thinking_ensemble_enabled",
    "thinking_ensemble_status",
]
