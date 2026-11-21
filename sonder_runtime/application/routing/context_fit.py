"""Context-length routing: never send a request to a model that cannot hold it.

Before a chat turn runs, its prompt (history plus the new message) is
estimated with the runtime's cheap estimator
(:func:`sonder_runtime.application.routing.long_context_overflow.estimate_prompt_tokens`,
the context-health estimator; no tokenizer).  The estimate is compared with
the context length of the model the turn's first rung is bound to:

* a Sonder Inference tier: the ``context_length`` its launch profile
  publishes on ``/v1/models``;
* an Ollama tier: the tier's configured ``num_ctx``
  (``SONDER_OLLAMA_TIER_OPTIONS``), else the model's own context length from
  Ollama's metadata.

Outcomes:

* **fits, or the context length is unknown** -- nothing changes; the turn
  routes exactly as it did before this module existed;
* **does not fit and a long-context tier is set** (``routing.long_context_tier``
  or ``SONDER_LONG_CONTEXT_TIER``) -- the turn runs on that tier instead, and
  the route reason says so (receipt notice, ``route.changed`` event, activity);
* **does not fit and no long-context tier can hold it** -- the turn fails
  before any model call with an error that says what to do.  The prompt is
  never truncated silently.

This module decides only: it performs no I/O and reads no environment.  The
adapter (``adapters/inference/context_fit_route.py``) supplies each tier's
window and the long-context tier in force.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ROUTE = "context_fit"
REASON_CODE = "context_exceeds_model"
# Estimates at or below this never trigger a lookup: every model Runtime binds
# serves at least this many tokens, so the common short turn costs nothing.
PROBE_FLOOR_TOKENS = 4096

SOURCE_PROFILE = "inference_profile"
SOURCE_TIER_OPTION = "tier_num_ctx"
SOURCE_MODEL = "model_metadata"
SOURCE_UNKNOWN = "unknown"

STATUS_SWITCHED = "switched"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class TierWindow:
    """The context window one tier's requests are served with."""

    tier: str
    model: str
    provider: str
    context_tokens: int | None
    source: str = SOURCE_UNKNOWN

    def describe(self) -> str:
        where = "%s on tier %s (%s)" % (self.model or "(unbound)", self.tier, self.provider)
        if self.context_tokens is None:
            return where + ", context length unknown"
        return where + ", %d-token context from %s" % (
            self.context_tokens, self.source.replace("_", " "),
        )

    def holds(self, tokens: int) -> bool | None:
        """True/False when the length is known, ``None`` when it is unknown."""
        if self.context_tokens is None:
            return None
        return int(tokens) <= int(self.context_tokens)


def _kilo(value: int) -> str:
    text = "%.1f" % (max(0, int(value)) / 1000.0)
    return (text[:-2] if text.endswith(".0") else text) + "k"


@dataclass(frozen=True)
class FitDecision:
    """What context-fit routing did for one turn."""

    status: str  # STATUS_SWITCHED | STATUS_REJECTED
    start: TierWindow
    estimated_tokens: int
    target: TierWindow | None = None
    reason: str = ""

    @property
    def switched(self) -> bool:
        return self.status == STATUS_SWITCHED

    def notice(self) -> str:
        """The one-line route reason."""
        if self.switched and self.target is not None:
            return (
                "context fit: routed to long-context tier %s (%s) -- request ~%s tokens "
                "> %s context of %s on tier %s" % (
                    self.target.tier, self.target.model or "(unbound)",
                    _kilo(self.estimated_tokens), _kilo(self.start.context_tokens or 0),
                    self.start.model or "(unbound)", self.start.tier,
                )
            )
        return "context fit: request ~%s tokens does not fit %s" % (
            _kilo(self.estimated_tokens), self.start.describe(),
        )

    def error_text(self) -> str:
        """The actionable failure for a turn no configured tier can hold."""
        lines = [
            "this request is about %d tokens (estimated), which does not fit %s; "
            "it was not sent, so nothing was truncated." % (
                self.estimated_tokens, self.start.describe(),
            ),
        ]
        if self.reason:
            lines.append(self.reason.rstrip(".") + ".")
        lines.append(
            "Fix: shorten the conversation (start a new chat or trim history), or "
            "bind a long-context model and set it as the long-context tier "
            "(runtime_policy_update routing_json='{\"long_context_tier\": \"<tier>\"}' "
            "or SONDER_LONG_CONTEXT_TIER=<tier>); see docs/integration/context-fit-routing.md."
        )
        return " ".join(lines)

    def receipt(self) -> dict[str, object]:
        """Bounded, content-free receipt entry (``sonder_receipt.overflow``)."""
        entry: dict[str, object] = {
            "status": self.status,
            "notice": self.notice()[:300],
            "route": ROUTE,
            "from_model": str(self.start.model or "")[:120],
            "from_tier": self.start.tier[:32],
            "estimated_tokens": int(self.estimated_tokens),
        }
        if self.start.context_tokens is not None:
            entry["context_tokens"] = int(self.start.context_tokens)
        if self.target is not None:
            entry["to_model"] = str(self.target.model or "")[:120]
            entry["to_tier"] = self.target.tier[:32]
        if self.reason and not self.switched:
            entry["reason"] = self.reason[:200]
        return entry

    def telemetry(self) -> dict[str, object]:
        """``route.changed`` attributes (identifiers and counts only)."""
        target = self.target or self.start
        return {
            "from_provider": self.start.provider,
            "from_model": self.start.model,
            "to_provider": target.provider,
            "to_model": target.model,
            "reason_code": REASON_CODE,
            "estimated_tokens": int(self.estimated_tokens),
            "threshold": int(self.start.context_tokens or 0),
        }


def decide(
    estimated_tokens: int,
    start: TierWindow,
    *,
    long_context_tier: str,
    window_for: Callable[[str], TierWindow],
    evidence_failure: Callable[[TierWindow], str] | None = None,
) -> FitDecision | None:
    """The context-fit decision for a turn about to run on ``start``.

    ``None`` means route exactly as before: the estimate fits, or the start
    tier's context length is unknown.  ``window_for`` resolves the
    long-context tier's window; ``evidence_failure`` returns a reason when
    capability evidence says that tier's model fails long context (then it is
    not used).  A long-context tier whose own length is unknown is used: the
    operator named it for long context.
    """
    estimated = max(0, int(estimated_tokens or 0))
    if start.holds(estimated) is not False:
        return None
    tier = str(long_context_tier or "").strip().lower()
    if not tier:
        return FitDecision(
            STATUS_REJECTED, start, estimated,
            reason="no long-context tier is configured",
        )
    if tier == start.tier:
        return FitDecision(
            STATUS_REJECTED, start, estimated,
            reason="the long-context tier is this same tier",
        )
    try:
        target = window_for(tier)
    except Exception as exc:  # noqa: BLE001 - a lookup failure is reported, not raised
        return FitDecision(
            STATUS_REJECTED, start, estimated,
            reason="the long-context tier %s could not be resolved (%s)" % (tier, type(exc).__name__),
        )
    if not target.model:
        return FitDecision(
            STATUS_REJECTED, start, estimated, target,
            reason="the long-context tier %s has no model bound" % tier,
        )
    if target.holds(estimated) is False:
        return FitDecision(
            STATUS_REJECTED, start, estimated, target,
            reason="the long-context tier does not hold it either: %s" % target.describe(),
        )
    if evidence_failure is not None:
        why = evidence_failure(target)
        if why:
            return FitDecision(
                STATUS_REJECTED, start, estimated, target,
                reason="the long-context tier %s is excluded by capability evidence (%s)"
                % (tier, why),
            )
    return FitDecision(STATUS_SWITCHED, start, estimated, target)


__all__ = [
    "FitDecision",
    "PROBE_FLOOR_TOKENS",
    "REASON_CODE",
    "ROUTE",
    "SOURCE_MODEL",
    "SOURCE_PROFILE",
    "SOURCE_TIER_OPTION",
    "SOURCE_UNKNOWN",
    "STATUS_REJECTED",
    "STATUS_SWITCHED",
    "TierWindow",
    "decide",
]
