"""Runtime guard: steer runs of single read-only calls to a registered batch form.

Issue #510 section 6 ("batching/coalescing guard where repeated single
operations are wasteful").

The repeat guards in the agent loop catch the *same* call issued again.  They
do not see a model that reads twelve different files with twelve separate
``file_read`` calls, spending one model decision and one tool round trip per
file, when a single bounded ``context_pack`` call returns the same guarded
reads in one step.  This guard counts distinct targets of one single-target
read-only tool inside one agent turn window and responds in two bounded,
materially different stages:

* at ``advisory_after`` distinct targets it attaches a typed
  :class:`BatchAdvisory` naming the batch tool and the exact argument to use;
  the single call's own result is returned unchanged;
* once ``refuse_after`` distinct targets have been read singly, a further
  single call on a *new* target is not dispatched; the caller receives a typed
  :class:`BatchRefusal` naming the batch tool with the refused target already
  placed in its argument.  After ``max_refusals`` refusals in one window the
  refusal is marked ``exhausted`` and the caller ends the run instead of
  spending the remaining step budget on ignored steering.

What the guard never does: it never coalesces or rewrites a call itself, never
changes a dispatched result, and never applies to a tool that can mutate
state or execute code.  A counterpart is only active when both halves are
registered, classified read-only, and absent from the caller's mutating and
execution sets (:func:`resolve_counterparts`), and when the caller confirms the
batch tool is admissible for this run (``batch_admissible``) -- the guard must
never steer a model to a tool a later gate would refuse.

What restarts the window: a new agent turn (a new guard instance),
:meth:`BatchCoalescingGuard.reset` (callers use it after any state-changing
step, because re-reading after a change is legitimate), or a successful call
of the family's batch tool (the model complied).  Re-reading an
already-counted target is left to the existing identical-call guards and is
never counted or refused here.

The module is pure: no I/O, no clock, no environment access.
:meth:`BatchCoalescingConfig.from_environ` takes an explicit mapping.
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping


GUARD_NAME = "batch_coalescing"

ENV_ADVISORY_AFTER = "SONDER_AGENT_BATCH_ADVISORY_AFTER"
ENV_REFUSE_AFTER = "SONDER_AGENT_BATCH_REFUSE_AFTER"
ENV_MAX_REFUSALS = "SONDER_AGENT_BATCH_MAX_REFUSALS"

DEFAULT_ADVISORY_AFTER = 3
DEFAULT_REFUSE_AFTER = 6
DEFAULT_MAX_REFUSALS = 3

# Configuration ceilings.  A window can never need more single reads than a
# turn has steps (the agent loop clamps max_steps to 20), so larger values are
# indistinguishable from "off" and are refused rather than silently accepted.
MIN_ADVISORY_AFTER = 2
MAX_THRESHOLD = 20
MAX_REFUSALS_CEILING = 10

@dataclass(frozen=True)
class BatchCounterpart:
    """One single-target read-only tool and the batch tool that subsumes it.

    ``target_argument`` names the single tool's target field; ``batch_argument``
    names the batch tool's list field that takes the same targets.
    """

    single_tool: str
    batch_tool: str
    target_argument: str
    batch_argument: str
    family: str

    def __post_init__(self) -> None:
        for name in ("single_tool", "batch_tool", "target_argument",
                     "batch_argument", "family"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value.strip() != value:
                raise ValueError("batch counterpart %s must be a non-empty trimmed string" % name)
        if self.single_tool == self.batch_tool:
            raise ValueError("a batch counterpart must name two different tools")


# Declared candidates.  A candidate is only a claim that the batch tool
# performs the same guarded operation over a list of the single tool's
# targets; :func:`resolve_counterparts` activates it only against the tools the
# host actually registers.  ``context_pack`` reads each path through the same
# containment, symlink, sensitive-file and approval policy as ``file_read``
# and reports per-file errors without aborting later reads.
CANDIDATE_COUNTERPARTS: tuple[BatchCounterpart, ...] = (
    BatchCounterpart(
        single_tool="file_read",
        batch_tool="context_pack",
        target_argument="path",
        batch_argument="paths_json",
        family="file-read",
    ),
)


def resolve_counterparts(
    candidates: Iterable[BatchCounterpart],
    *,
    registered: Iterable[str],
    read_only: Iterable[str],
    state_changing: Iterable[str],
) -> tuple[BatchCounterpart, ...]:
    """Keep only candidates whose both halves are registered read-only tools.

    ``registered`` is the host's dispatchable tool set, ``read_only`` its
    read-only classification, and ``state_changing`` every tool that can
    mutate state or execute code.  A tool in ``state_changing`` disqualifies
    the candidate even if it is also listed read-only: the guard fails closed
    toward *not* steering.  Duplicate single tools keep the first candidate.
    """
    registered_set = frozenset(registered)
    read_only_set = frozenset(read_only)
    changing_set = frozenset(state_changing)
    active: list[BatchCounterpart] = []
    seen: set[str] = set()
    for candidate in candidates:
        tools = (candidate.single_tool, candidate.batch_tool)
        if candidate.single_tool in seen:
            continue
        if not all(tool in registered_set for tool in tools):
            continue
        if not all(tool in read_only_set for tool in tools):
            continue
        if any(tool in changing_set for tool in tools):
            continue
        seen.add(candidate.single_tool)
        active.append(candidate)
    return tuple(active)


def _strict_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % name)
    return value


@dataclass(frozen=True)
class BatchCoalescingConfig:
    """Typed thresholds with safe defaults.

    ``advisory_after`` distinct single-target calls attach an advisory;
    after ``refuse_after`` distinct targets further singles on new targets are
    refused; the ``max_refusals``-th refusal in one window is ``exhausted``.
    """

    advisory_after: int = DEFAULT_ADVISORY_AFTER
    refuse_after: int = DEFAULT_REFUSE_AFTER
    max_refusals: int = DEFAULT_MAX_REFUSALS

    def __post_init__(self) -> None:
        advisory = _strict_int(self.advisory_after, "advisory_after")
        refuse = _strict_int(self.refuse_after, "refuse_after")
        refusals = _strict_int(self.max_refusals, "max_refusals")
        if not MIN_ADVISORY_AFTER <= advisory <= MAX_THRESHOLD:
            raise ValueError(
                "advisory_after must be between %d and %d" % (MIN_ADVISORY_AFTER, MAX_THRESHOLD)
            )
        if not advisory <= refuse <= MAX_THRESHOLD:
            raise ValueError(
                "refuse_after must be between advisory_after and %d" % MAX_THRESHOLD
            )
        if not 1 <= refusals <= MAX_REFUSALS_CEILING:
            raise ValueError("max_refusals must be between 1 and %d" % MAX_REFUSALS_CEILING)

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> BatchCoalescingConfig:
        """Parse the three optional settings; an unset one keeps its default.

        A present value must be a plain decimal integer inside the ceilings;
        anything else raises ``ValueError`` so the caller can decide how to
        fail closed instead of receiving a silently different threshold.
        """
        values = {}
        for key, attribute, default in (
            (ENV_ADVISORY_AFTER, "advisory_after", DEFAULT_ADVISORY_AFTER),
            (ENV_REFUSE_AFTER, "refuse_after", DEFAULT_REFUSE_AFTER),
            (ENV_MAX_REFUSALS, "max_refusals", DEFAULT_MAX_REFUSALS),
        ):
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                values[attribute] = default
                continue
            text = raw.strip()
            if not text.isdecimal() or not text.isascii():
                raise ValueError("%s must be a positive integer" % key)
            values[attribute] = int(text)
        return cls(**values)


def _batch_example(counterpart: BatchCounterpart, targets: Iterable[str]) -> str:
    return "%s %s" % (
        counterpart.batch_tool,
        json.dumps({counterpart.batch_argument: list(targets)}, ensure_ascii=False),
    )


@dataclass(frozen=True)
class BatchAdvisory:
    """Typed steering attached after a successful single-target call."""

    counterpart: BatchCounterpart
    distinct_targets: int
    refuse_after: int
    guard: str = GUARD_NAME

    def render(self) -> str:
        cp = self.counterpart
        return (
            "HOST BATCH ADVISORY (%s): %d distinct targets have now been read "
            "with one %s call each in this turn. The result above is unchanged. "
            "Request every remaining target you need in one %s call, for "
            "example: %s. After %d distinct single %s targets in this window, "
            "further single %s calls on new targets are refused."
            % (
                self.guard, self.distinct_targets, cp.single_tool, cp.batch_tool,
                _batch_example(cp, ["<next path>", "<another path>"]),
                self.refuse_after, cp.single_tool, cp.single_tool,
            )
        )

    def telemetry(self) -> dict:
        return {
            "guard": self.guard,
            "action": "advisory",
            "family": self.counterpart.family,
            "tool": self.counterpart.single_tool,
            "batch_tool": self.counterpart.batch_tool,
            "distinct_targets": self.distinct_targets,
            "threshold": self.refuse_after,
        }


@dataclass(frozen=True)
class BatchRefusal:
    """Typed refusal: the single call was not dispatched."""

    counterpart: BatchCounterpart
    target: str
    distinct_targets: int
    refuse_after: int
    refusals: int
    max_refusals: int
    guard: str = GUARD_NAME

    @property
    def exhausted(self) -> bool:
        return self.refusals >= self.max_refusals

    def render(self) -> str:
        cp = self.counterpart
        text = (
            "ERROR: HOST BATCH GUARD (%s): %s was not run. %d distinct targets "
            "were already read one %s call at a time in this turn (limit %d). "
            "Request this target together with every other target you still "
            "need in one %s call: %s. Re-reading a target that was already "
            "read is not affected. Refusal %d of %d in this window."
            % (
                self.guard, cp.single_tool, self.distinct_targets, cp.single_tool,
                self.refuse_after, cp.batch_tool, _batch_example(cp, [self.target]),
                self.refusals, self.max_refusals,
            )
        )
        if self.exhausted:
            text += " The refusal limit is reached; the run ends here."
        return text

    def telemetry(self) -> dict:
        return {
            "guard": self.guard,
            "action": "exhausted" if self.exhausted else "refusal",
            "family": self.counterpart.family,
            "tool": self.counterpart.single_tool,
            "batch_tool": self.counterpart.batch_tool,
            "distinct_targets": self.distinct_targets,
            "threshold": self.refuse_after,
            "refusals": self.refusals,
        }


def target_key(value) -> str | None:
    """Normalize one target for distinctness, or ``None`` when uncountable.

    Separators are unified and the path is normalized and case-folded, so
    spellings of one file never count as several targets.  Case folding can
    only merge targets (undercount), never split one, so the guard errs
    toward allowing a call.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return posixpath.normpath(text.replace("\\", "/")).casefold()


class BatchCoalescingGuard:
    """Per-turn window over single-target read-only calls.

    One instance covers one agent turn; callers construct a new instance per
    turn, so a new turn always starts with an empty window.
    """

    def __init__(
        self,
        counterparts: Iterable[BatchCounterpart],
        config: BatchCoalescingConfig | None = None,
        *,
        batch_admissible: Callable[[BatchCounterpart, str], bool] | None = None,
    ) -> None:
        self.config = config or BatchCoalescingConfig()
        self._by_single: dict[str, BatchCounterpart] = {}
        self._by_batch: dict[str, list[BatchCounterpart]] = {}
        for counterpart in counterparts:
            self._by_single.setdefault(counterpart.single_tool, counterpart)
            self._by_batch.setdefault(counterpart.batch_tool, []).append(counterpart)
        self._admissible = batch_admissible or (lambda _counterpart, _target: True)
        # family -> normalized keys of targets read singly in this window
        self._targets: dict[str, set[str]] = {}
        self._refusals = 0
        self._stats = {"advisories": 0, "refusals": 0, "resets": 0}

    @property
    def active(self) -> bool:
        return bool(self._by_single)

    def _window(self, counterpart: BatchCounterpart) -> set[str]:
        return self._targets.setdefault(counterpart.family, set())

    def before_dispatch(self, tool: str, args) -> BatchRefusal | None:
        """Return a refusal when this single call must not be dispatched."""
        counterpart = self._by_single.get(tool)
        if counterpart is None:
            return None
        raw = args.get(counterpart.target_argument) if isinstance(args, dict) else None
        key = target_key(raw)
        if key is None:
            return None
        window = self._targets.get(counterpart.family, ())
        if key in window or len(window) < self.config.refuse_after:
            return None
        if not self._admissible(counterpart, raw.strip()):
            return None
        self._refusals += 1
        self._stats["refusals"] += 1
        return BatchRefusal(
            counterpart=counterpart,
            target=raw.strip(),
            distinct_targets=len(window),
            refuse_after=self.config.refuse_after,
            refusals=self._refusals,
            max_refusals=self.config.max_refusals,
        )

    def after_dispatch(self, tool: str, args, *, ok: bool) -> BatchAdvisory | None:
        """Account for a dispatched call; return an advisory when one is due.

        Only successful calls count: a failed single read did no wasteful work
        that a batch would have saved, and failure streaks already have their
        own no-progress guard.
        """
        if not ok:
            return None
        for counterpart in self._by_batch.get(tool, ()):
            self._targets.pop(counterpart.family, None)
        counterpart = self._by_single.get(tool)
        if counterpart is None:
            return None
        raw = args.get(counterpart.target_argument) if isinstance(args, dict) else None
        key = target_key(raw)
        if key is None:
            return None
        window = self._window(counterpart)
        if key in window:
            return None
        window.add(key)
        if len(window) < self.config.advisory_after:
            return None
        if not self._admissible(counterpart, raw.strip()):
            return None
        self._stats["advisories"] += 1
        return BatchAdvisory(
            counterpart=counterpart,
            distinct_targets=len(window),
            refuse_after=self.config.refuse_after,
        )

    def reset(self) -> None:
        """Start a new window (after a state-changing step)."""
        if self._targets:
            self._stats["resets"] += 1
        self._targets.clear()

    def snapshot(self) -> dict:
        """Bounded telemetry for status surfaces and tests."""
        return {
            "guard": GUARD_NAME,
            "families": {family: len(keys) for family, keys in self._targets.items()},
            "advisories": self._stats["advisories"],
            "refusals": self._stats["refusals"],
            "resets": self._stats["resets"],
            "advisory_after": self.config.advisory_after,
            "refuse_after": self.config.refuse_after,
            "max_refusals": self.config.max_refusals,
        }
