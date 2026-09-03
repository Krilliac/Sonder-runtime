"""Bounded automatic tier escalation for the default model route.

The capability router (domain) knows which tiers suit a task and in which
order to try them, and the model-gateway policy (application) bounds how many
times one request may step up.  Until now nothing connected either to a live
call: a chat turn or an agent run that failed on its first model returned the
failure.  This module is the planning half of that connection.  Given the
target the caller already resolved, the tiers the runtime policy binds, and a
resolver from tier name to concrete target, it builds the ordered list of
distinct-model attempts one turn may spend, classifies an attempt's outcome,
and describes what happened for the operator and the activity record.

No I/O, no environment reads (the entry layer reads ``SONDER_MODEL_ESCALATION``
and passes the verdict in), no model calls.  The server owns the loop; this
module owns the decisions so tests can pin them without a model.

Rules:

- Only the *default* route escalates.  An explicit tier, an exact model pin, a
  cloud target or a caller-selected OpenAI ``model`` is a routing contract and
  is never rerouted.
- Every rung is a locally bound tier from the router's ladder for the task
  class, deduplicated by the concrete model (two tiers bound to the same
  model are one rung; with every tier on one model the plan is one rung and
  behaviour is exactly what it was).
- Escalation only moves up: a ladder tier no stronger by role than the start
  (``fast`` after a ``code`` start) is never a rung, and the vision tier is
  only ever a rung for a request that carries an image.
- The first rung is the resolved default target.  A reasoning-class prompt
  starts on a bound ``reasoning`` tier when that tier resolves to a different
  model; vision only pre-routes with a real image signal (a keyword guess
  would hand text work to a model that answers with end-of-sequence).
- At most :data:`MAX_ESCALATIONS` additional attempts, the same ceiling the
  gateway escalation policy enforces.
- Only a failed or empty attempt steps up.  A satisfied answer never spends a
  stronger model; a cancellation or an exhausted budget is never retried.
  The one verifier a chat answer has is the execution-grounded code gate:
  runnable code that still fails after its repair round-trip is a failed
  attempt too (:func:`verifier_reason`).
  For the workbench agent a completion claim that changed nothing and ran
  no validation counts as a failure when the request asked for a change or
  a check (the entry layer decides that from the request's action verbs).
- The default route's augmentation (facts, lessons, recall) travels with the
  prompt to every rung: the caller asked for the learning route, the runtime
  substitutes a stronger *local* model for the same job, and the privacy
  contract is unchanged because cloud rungs are never planned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from sonder_runtime.application.model_gateway.escalation import MAX_ESCALATIONS
from sonder_runtime.domain.routing import capability_router
from sonder_runtime.domain.runtime_policy import rules as policy_rules

# Environment knob the entry layer reads and hands to :func:`enabled`.
KNOB = "SONDER_MODEL_ESCALATION"
_OFF_TOKENS = frozenset({"0", "false", "no", "off", "disabled", "none"})

# Escalation reasons this module reports; both are in the router's vocabulary.
REASON_FAILED = "failed"
REASON_EMPTY = "empty_response"
assert REASON_FAILED in capability_router.ESCALATION_REASONS
assert REASON_EMPTY in capability_router.ESCALATION_REASONS

# Model-call failure kinds that never step up: a cancellation is a caller's
# control signal and an exhausted budget is a ceiling, not a weaker model.
NON_ESCALATING_KINDS = frozenset({"cancelled", "budget"})

# Strength by tier role, the only ordering the runtime has without measuring
# the bound models: the quick tier sits below the working tiers, the reasoning
# tier above them.  An unknown label (the default ``sonder`` route, an exact
# model pin) counts as a working tier.
_STRENGTH = {"fast": 0, "code": 1, "general": 1, "vision": 1, "reasoning": 2}
_WORKING_STRENGTH = 1


def enabled(value) -> bool:
    """Whether the knob's raw value (``None`` when unset) leaves escalation on."""
    text = str(value if value is not None else "").strip().lower()
    return text not in _OFF_TOKENS


@dataclass(frozen=True)
class Rung:
    """One concrete attempt target: the tier label recorded on the turn and
    the model it resolves to."""

    tier: str
    model: str
    cloud: bool = False
    augment: bool = True

    def label(self) -> str:
        return "%s (%s)" % (self.tier, self.model)


@dataclass(frozen=True)
class Plan:
    """The ordered attempts one turn may spend; ``rungs[0]`` is always tried."""

    task: str
    confidence: float
    rungs: tuple[Rung, ...]
    prerouted: bool = False

    def __post_init__(self) -> None:
        if not self.rungs:
            raise ValueError("an escalation plan needs at least one rung")

    @property
    def start(self) -> Rung:
        return self.rungs[0]

    @property
    def escalations(self) -> int:
        return len(self.rungs) - 1

    def next_rung(self, index: int) -> Rung | None:
        """The rung after attempt ``index`` (0-based), or ``None`` when spent."""
        following = index + 1
        return self.rungs[following] if 0 <= following < len(self.rungs) else None


@dataclass(frozen=True)
class Step:
    """One escalation that happened: attempt ``attempt`` (1-based) on
    ``from_rung`` ended with ``reason`` and the turn moved to ``to_rung``."""

    attempt: int
    reason: str
    from_rung: Rung
    to_rung: Rung
    detail: str = ""

    def summary(self) -> str:
        text = "%s -> %s: %s" % (self.from_rung.label(), self.to_rung.label(), self.reason)
        return "%s (%s)" % (text, self.detail) if self.detail else text


def single(start: Rung) -> Plan:
    """A plan that never escalates (explicit targets, cloud, knob off)."""
    return Plan(task="", confidence=0.0, rungs=(start,))


def plan(
    prompt: str,
    *,
    start: Rung,
    available: Iterable[str],
    resolve: Callable[[str], Rung | None],
    has_image: bool = False,
    max_escalations: int = MAX_ESCALATIONS,
) -> Plan:
    """Build the bounded attempt list for one default-route turn.

    ``available`` is the set of tiers the runtime policy binds; ``resolve``
    maps a tier name to its rung (``None`` for an unbound, cloud or otherwise
    unusable tier).  Rungs are distinct by model and never weaker by role
    than ``start``; ``start`` is kept even when a specialist rung is placed
    before it so the plan can fall back to the route the caller asked for.
    """
    if start.cloud:
        return single(start)
    budget = max(0, min(int(max_escalations), MAX_ESCALATIONS))
    bound = tuple(dict.fromkeys(str(tier or "").strip().lower() for tier in available if tier))
    route = capability_router.route(prompt, bound, has_image=has_image)

    ordered: list[str] = []
    prerouted_tier = None
    specialist = route.tier
    if (
        specialist in policy_rules.OPTIONAL_LOCAL_TIERS
        and specialist in bound
        and not (specialist == "vision" and not has_image)
    ):
        candidate = resolve(specialist)
        if candidate is not None and not candidate.cloud and not _same_model(candidate, start):
            ordered.append(specialist)
            prerouted_tier = specialist
    ordered.append("")  # the start rung's slot
    ordered.extend(route.ladder)

    floor = _STRENGTH.get(str(start.tier or "").strip().lower(), _WORKING_STRENGTH)
    rungs: list[Rung] = []
    for tier in ordered:
        if tier == "":
            rung = start
        else:
            if tier == "vision" and not has_image:
                continue
            if tier != prerouted_tier and _STRENGTH.get(tier, _WORKING_STRENGTH) < floor:
                continue
            rung = resolve(tier)
            if rung is None or rung.cloud:
                continue
            rung = Rung(tier=rung.tier or tier, model=rung.model, cloud=False, augment=start.augment)
        if not rung.model or any(_same_model(rung, seen) for seen in rungs):
            continue
        rungs.append(rung)
        if len(rungs) >= 1 + budget:
            break
    if not rungs:
        rungs.append(start)
    return Plan(
        task=route.task, confidence=route.confidence, rungs=tuple(rungs),
        prerouted=prerouted_tier is not None,
    )


def _same_model(left: Rung, right: Rung) -> bool:
    return str(left.model or "").strip().casefold() == str(right.model or "").strip().casefold()


def failure_reason(error=None, response=None) -> str | None:
    """Classify one attempt: an escalation reason, or ``None`` when the
    attempt stands (answered, cancelled, budget-bound, or a cloud failure).

    ``error`` is duck-typed on the transport error's ``kind``/``cloud`` fields
    so this layer never imports the adapter that defines it.
    """
    if error is not None:
        kind = str(getattr(error, "kind", "") or "").strip().lower()
        if bool(getattr(error, "cloud", False)) or kind in NON_ESCALATING_KINDS:
            return None
        return REASON_EMPTY if kind == "empty_response" else REASON_FAILED
    if response is None:
        return None
    return REASON_EMPTY if not str(response).strip() else None


VERIFIER_DETAIL = "runnable code failed verification after a repair"


def verifier_reason(verified) -> str | None:
    """Classify a code-gate verdict: ``False`` (runnable code still failing
    after the repair round-trip) steps up; ``True`` and ``None`` (nothing to
    gate, or inconclusive) stand."""
    return REASON_FAILED if verified is False else None


def describe(steps: Iterable[Step]) -> str:
    """One operator-facing line for the escalations a turn spent ('' if none)."""
    parts = [step.summary() for step in steps]
    return "model escalation: " + "; ".join(parts) if parts else ""


__all__ = [
    "KNOB", "MAX_ESCALATIONS", "NON_ESCALATING_KINDS", "Plan", "REASON_EMPTY",
    "REASON_FAILED", "Rung", "Step", "VERIFIER_DETAIL", "describe", "enabled",
    "failure_reason", "plan", "single", "verifier_reason",
]
