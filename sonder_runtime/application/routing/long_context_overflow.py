"""Opt-in long-context overflow for the chat rung.

A dense local model that is fast at short context can spill out of VRAM and
slow sharply once a conversation grows past a few tens of thousands of
tokens, while a mixture-of-experts model keeps its speed there.  When the
operator enables the overflow (``/runtime overflow on`` or
``SONDER_LONG_CONTEXT_OVERFLOW=1``) and a turn's estimated prompt exceeds the
threshold, the chat rung moves from its local tier to the overflow model on
the Ollama worker pool, so it runs on whichever worker advertises that model.

The runtime always says so: the decision becomes a one-line notice in the
response receipt (never in the answer text), a ``route.changed`` event and an
activity event, and the rung's system prompt names the model that actually
answers.  When the overflow model is unavailable the turn keeps its route and
the notice says why.

Rules:

- Off unless enabled; never for a cloud target, an exact model pin, the
  vision tier or anything that is not a local chat tier.
- The estimate is the runtime's usual cheap one
  (:func:`sonder_runtime.domain.context_formatting.rough_token_count`, the
  context-health estimator) over the system prompt, the history and the
  prompt.  Retrieval augmentation added later is not counted, so the estimate
  is a floor.
- Only the plan's first rung is rebound.  The original rung stays in the plan
  directly after the overflow rung, so an overflow attempt that fails (the
  pool rejects it, the worker is down) falls back to the route the turn had,
  and the notice then reports that it stayed there.

No I/O and no environment reads: the entry layer passes the settings, the
estimate and an availability probe in.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Iterator, Mapping

from sonder_runtime.application.routing import tier_escalation
from sonder_runtime.domain.context_formatting import rough_token_count

ROUTE = "long_context_overflow"
REASON_CODE = "context_over_threshold"
TARGET_PROVIDER = "ollama"
# Local chat tiers the overflow may rebind.  ``sonder`` is the default local
# alias route.  Vision is excluded: the overflow model may not take images.
ELIGIBLE_TIERS = frozenset({"sonder", "fast", "code", "general", "reasoning"})
# Output room and estimate slack when sizing the overflow model's window.
_OUTPUT_RESERVE = 1024
_WINDOW_STEP = 4096

AVAILABLE = "available"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Availability:
    """Whether a worker can serve the overflow model.

    ``unknown`` means some worker has not reported its models yet; the rung
    is tried and falls back to the original route if the pool rejects it.
    ``worker`` is for people (the worker's ``host:port``); ``worker_id`` is
    the pool's opaque id, for telemetry correlation only.
    """

    state: str
    worker: str = ""
    reason: str = ""
    worker_id: str = ""


def _kilo(value: int) -> str:
    text = "%.1f" % (max(0, int(value)) / 1000.0)
    return (text[:-2] if text.endswith(".0") else text) + "k"


@dataclass(frozen=True)
class Decision:
    """What the overflow did for one turn."""

    status: str  # "switched" | "unavailable"
    from_model: str
    from_provider: str
    to_model: str
    estimated_tokens: int
    threshold_tokens: int
    worker: str = ""
    reason: str = ""
    to_provider: str = TARGET_PROVIDER
    worker_id: str = ""

    @property
    def switched(self) -> bool:
        return self.status == "switched"

    def stayed(self, reason: str, model: str = "") -> "Decision":
        """The overflow rung did not answer; the turn stayed on ``model``."""
        return replace(
            self, status="unavailable", reason=str(reason or "overflow attempt failed"),
            from_model=str(model or self.from_model),
        )

    def notice(self) -> str:
        context = "context %s tokens > %s threshold" % (
            _kilo(self.estimated_tokens), _kilo(self.threshold_tokens),
        )
        if self.switched:
            return "long-context overflow: switched to %s on %s — %s" % (
                self.to_model, self.worker or "the Ollama pool", context,
            )
        target = " (%s)" % self.to_model if self.to_model else ""
        return "long-context overflow%s unavailable, stayed on %s: %s — %s" % (
            target, self.from_model, self.reason or "unavailable", context,
        )

    def receipt(self) -> dict:
        """Bounded, content-free receipt entry (``sonder_receipt.overflow``)."""
        entry = {
            "status": self.status,
            "notice": self.notice()[:300],
            "from_model": self.from_model[:120],
            "to_model": self.to_model[:120],
            "estimated_tokens": int(self.estimated_tokens),
            "threshold_tokens": int(self.threshold_tokens),
        }
        if self.worker:
            entry["worker"] = self.worker[:120]
        if self.reason and not self.switched:
            entry["reason"] = self.reason[:200]
        return entry

    def telemetry(self) -> dict:
        """``route.changed`` attributes (identifiers and counts only)."""
        fields = {
            "from_provider": self.from_provider,
            "from_model": self.from_model,
            "to_provider": self.to_provider,
            "to_model": self.to_model,
            "reason_code": REASON_CODE,
            "estimated_tokens": int(self.estimated_tokens),
            "threshold": int(self.threshold_tokens),
        }
        if self.worker_id:
            fields["to_worker_id"] = self.worker_id
        return fields


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, Mapping)
        )
    return "" if content is None else str(content)


def estimate_prompt_tokens(
    prompt: str, history: Iterable[Mapping] | None = None, system: str = "",
) -> int:
    """Estimated prompt tokens for one turn, with the context-health estimator."""
    total = rough_token_count(system) + rough_token_count(prompt)
    for message in history or ():
        if isinstance(message, Mapping):
            total += rough_token_count(_message_text(message.get("content")))
    return total


def decide(
    settings: Mapping,
    *,
    rung: tier_escalation.Rung,
    provider: str | None,
    estimated_tokens: int,
    availability: Callable[[str], Availability],
    reasoning_model: str = "",
    explicit_pin: bool = False,
) -> Decision | None:
    """The overflow decision for a turn about to run on ``rung``.

    ``None`` means the overflow does not apply (disabled, under threshold,
    cloud, exact pin, ineligible tier, or already on the overflow model).
    ``provider`` is the rung's bound non-Ollama provider (``None`` = Ollama).
    """
    if not isinstance(settings, Mapping) or not settings.get("enabled"):
        return None
    threshold = int(settings.get("threshold_tokens") or 0)
    estimated = max(0, int(estimated_tokens or 0))
    if threshold <= 0 or estimated <= threshold:
        return None
    if rung.cloud or explicit_pin:
        return None
    if str(rung.tier or "").strip().lower() not in ELIGIBLE_TIERS:
        return None
    target = str(settings.get("model") or reasoning_model or "").strip()
    base = Decision(
        status="unavailable", from_model=str(rung.model or ""),
        from_provider=str(provider or TARGET_PROVIDER), to_model=target,
        estimated_tokens=estimated, threshold_tokens=threshold,
    )
    if not target:
        return replace(base, reason="no overflow model configured (set one or bind a reasoning tier)")
    if provider is None and target.casefold() == str(rung.model or "").strip().casefold():
        return None
    try:
        found = availability(target)
    except Exception:
        found = Availability(UNAVAILABLE, reason="worker availability check failed")
    if found.state == UNAVAILABLE:
        return replace(base, reason=found.reason or "no Ollama worker advertises %s" % target)
    return replace(base, status="switched", worker=found.worker, worker_id=found.worker_id)


def apply(plan: tier_escalation.Plan, decision: Decision | None) -> tier_escalation.Plan:
    """``plan`` with the overflow rung first and the original route kept after it."""
    if decision is None or not decision.switched:
        return plan
    start = plan.start
    overflow = tier_escalation.Rung(
        tier=start.tier, model=decision.to_model, cloud=False,
        augment=start.augment, route=ROUTE,
    )
    wanted = decision.to_model.casefold()
    rest = tuple(
        rung for rung in plan.rungs
        if str(rung.model or "").strip().casefold() != wanted or rung is start
    )
    return replace(plan, rungs=(overflow, *rest))


def is_overflow(rung) -> bool:
    return getattr(rung, "route", "") == ROUTE


def plan_turn(
    settings: Mapping,
    plan: tier_escalation.Plan,
    *,
    prompt: str,
    history: Iterable[Mapping] | None,
    provider_for: Callable[[tier_escalation.Rung], str | None],
    availability: Callable[[str], Availability],
    reasoning_model: str = "",
    explicit_pin: bool = False,
) -> tuple[tier_escalation.Plan, Decision | None]:
    """Decide the overflow for a turn and return the plan it runs.

    The estimate covers the history and the prompt; the system prompt and
    any retrieval augmentation are added later and are not counted.
    """
    if not isinstance(settings, Mapping) or not settings.get("enabled"):
        return plan, None
    decision = decide(
        settings, rung=plan.start, provider=provider_for(plan.start),
        estimated_tokens=estimate_prompt_tokens(prompt, history),
        availability=availability, reasoning_model=reasoning_model,
        explicit_pin=explicit_pin,
    )
    return apply(plan, decision), decision


def error_detail(rung, error) -> str:
    """Escalation detail for a failed overflow attempt ('' for other rungs)."""
    if not is_overflow(rung):
        return ""
    kind = str(getattr(error, "kind", "") or "").strip()
    detail = str(getattr(error, "detail", "") or "").strip()
    text = "%s: %s" % (kind, detail) if kind and detail and detail != kind else kind or detail
    return text[:160]


def failure_text(step) -> str:
    """Why the overflow rung gave way, from its escalation step ('' otherwise)."""
    if not is_overflow(getattr(step, "from_rung", None)):
        return ""
    detail = str(getattr(step, "detail", "") or "")
    reason = str(getattr(step, "reason", "") or "failed")
    return "overflow attempt %s%s" % (reason, " (%s)" % detail if detail else "")


def context_window(estimated_tokens: int, selected: int | None, *, ceiling: int) -> int:
    """A native window that holds the estimated prompt plus answer room.

    ``selected`` is the model-aware automatic choice, which for a large model
    is well below the threshold the overflow exists for; the window is raised
    to fit, never lowered, and never above ``ceiling``.
    """
    needed = int(max(0, int(estimated_tokens or 0)) * 1.25) + _OUTPUT_RESERVE
    needed = -(-needed // _WINDOW_STEP) * _WINDOW_STEP
    window = max(int(selected or 0), needed)
    return min(window, int(ceiling)) if ceiling else window


def settle(decision: Decision | None, answered, failure: str = "") -> Decision | None:
    """The decision as it turned out once ``answered`` produced the reply."""
    if decision is None or not decision.switched:
        return decision
    if is_overflow(answered):
        return decision
    return decision.stayed(failure or "overflow attempt failed", getattr(answered, "model", ""))


_NOTICES: ContextVar[list | None] = ContextVar("sonder_overflow_notices", default=None)


@contextmanager
def notice_scope() -> Iterator[list]:
    """Collect this turn's overflow decision for the response receipt."""
    notes: list = []
    token = _NOTICES.set(notes)
    try:
        yield notes
    finally:
        _NOTICES.reset(token)


def record(decision: Decision | None) -> bool:
    """Record the turn's final decision; False when no scope is bound."""
    notes = _NOTICES.get()
    if decision is None or notes is None:
        return False
    notes[:] = [decision]
    return True


def receipt_entry(notes: Iterable) -> dict | None:
    """The receipt entry for a scope's recorded decision, or ``None``."""
    for decision in reversed(list(notes or ())):
        if isinstance(decision, Decision):
            return decision.receipt()
    return None


__all__ = [
    "AVAILABLE", "Availability", "Decision", "ELIGIBLE_TIERS", "REASON_CODE", "ROUTE",
    "UNAVAILABLE", "UNKNOWN", "apply", "context_window", "decide", "error_detail",
    "estimate_prompt_tokens", "failure_text", "is_overflow", "notice_scope",
    "plan_turn", "receipt_entry", "record", "settle",
]
