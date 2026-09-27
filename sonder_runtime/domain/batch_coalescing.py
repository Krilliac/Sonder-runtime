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
  spending the remaining step budget on ignored steering.  The refusal count
  belongs to the window: when the window restarts, so does the count.

Both texts state the caller's model-observation budget (``view_chars``) and
recommend at most :func:`recommended_batch_size` targets per batch call, so
following the steering never hides a file behind a view clip.  A target the
model received through a successful batch call is *covered*: reading it
singly afterwards (for example to see a part the batch view clipped) is never
counted or refused.

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
of the family's batch tool (the model complied).  Re-reading a target that
is already counted, already covered by a successful batch call, or that
already failed in this window (a retry, which a host evidence contract may
demand) is left to the existing identical-call and no-progress guards and is
never counted or refused here.  Target identity comes from the caller's
``resolve_target`` (the host resolves relative, absolute and symlinked
spellings to one real path) followed by :func:`target_key`.

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

# Configuration ceilings.  The agent loop clamps one turn to at most
# ``AGENT_STEP_CEILING`` steps (the host pins its clamp to this value).  A
# refusal needs ``refuse_after`` successful single reads plus one more call,
# and an advisory is only useful when a later step can act on it, so a
# threshold above ``AGENT_STEP_CEILING - 1`` could never fire within a turn.
# Such values are indistinguishable from "off" and are refused rather than
# silently accepted.
AGENT_STEP_CEILING = 20
MIN_ADVISORY_AFTER = 2
MAX_THRESHOLD = AGENT_STEP_CEILING - 1
MAX_REFUSALS_CEILING = 10

# Minimum model-visible characters one batched target should keep.  The
# steering texts recommend no more targets per batch call than the caller's
# model-observation budget can show at this size each.
MIN_VIEW_CHARS_PER_TARGET = 1500

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


def recommended_batch_size(view_chars: int) -> int:
    """Targets per batch call that the model-observation budget can show.

    ``view_chars`` is the number of characters of one tool result the caller
    shows the model.  Each recommended target keeps at least
    ``MIN_VIEW_CHARS_PER_TARGET`` of that budget; the result is at least 1.
    """
    return max(1, int(view_chars) // MIN_VIEW_CHARS_PER_TARGET)


def _view_note(counterpart: BatchCounterpart, view_chars: int) -> str:
    if view_chars <= 0:
        return ""
    return (
        " One %s result is shown to you within %d characters shared across "
        "its targets, so request at most %d targets per call; a target that "
        "the view clips is marked, and reading a target already returned by "
        "a successful %s call with %s is never refused."
        % (
            counterpart.batch_tool, view_chars,
            recommended_batch_size(view_chars), counterpart.batch_tool,
            counterpart.single_tool,
        )
    )


@dataclass(frozen=True)
class BatchAdvisory:
    """Typed steering attached after a successful single-target call."""

    counterpart: BatchCounterpart
    distinct_targets: int
    refuse_after: int
    view_chars: int = 0
    guard: str = GUARD_NAME

    def render(self) -> str:
        cp = self.counterpart
        return (
            "HOST BATCH ADVISORY (%s): %d distinct targets have now been read "
            "with one %s call each in this window. The result above is "
            "unchanged. Request the remaining targets you need together in "
            "%s calls instead, for example: %s. After %d distinct single %s "
            "targets in this window, further single %s calls on new targets "
            "are refused.%s"
            % (
                self.guard, self.distinct_targets, cp.single_tool, cp.batch_tool,
                _batch_example(cp, ["<next path>", "<another path>"]),
                self.refuse_after, cp.single_tool, cp.single_tool,
                _view_note(cp, self.view_chars),
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
    view_chars: int = 0
    guard: str = GUARD_NAME

    @property
    def exhausted(self) -> bool:
        return self.refusals >= self.max_refusals

    def render(self) -> str:
        cp = self.counterpart
        text = (
            "ERROR: HOST BATCH GUARD (%s): %s was not run. %d distinct targets "
            "were already read one %s call at a time in this window (limit %d). "
            "Request this target with %s instead, together with other targets "
            "you still need: %s. Re-reading or retrying a target that was "
            "already read or attempted is not affected.%s Refusal %d of %d in "
            "this window; a successful %s call starts a new window."
            % (
                self.guard, cp.single_tool, self.distinct_targets, cp.single_tool,
                self.refuse_after, cp.batch_tool, _batch_example(cp, [self.target]),
                _view_note(cp, self.view_chars),
                self.refusals, self.max_refusals, cp.batch_tool,
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

    Separators are unified and the path is lexically normalized and
    case-folded, so lexically equivalent spellings of one file never count as
    several targets.  Case folding can only merge targets (undercount), never
    split one, so the guard errs toward allowing a call.  Relative versus
    absolute spellings and symlinks need the filesystem; the guard's
    ``resolve_target`` hook maps those to one real path before this runs.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return posixpath.normpath(text.replace("\\", "/")).casefold()


class BatchCoalescingGuard:
    """Per-turn windows over single-target read-only calls, one per family.

    One instance covers one agent turn; callers construct a new instance per
    turn, so a new turn always starts with empty windows.
    """

    def __init__(
        self,
        counterparts: Iterable[BatchCounterpart],
        config: BatchCoalescingConfig | None = None,
        *,
        batch_admissible: Callable[[BatchCounterpart, str], bool] | None = None,
        resolve_target: Callable[[str], str] | None = None,
        view_chars: int = 0,
    ) -> None:
        self.config = config or BatchCoalescingConfig()
        self._by_single: dict[str, BatchCounterpart] = {}
        self._by_batch: dict[str, list[BatchCounterpart]] = {}
        for counterpart in counterparts:
            self._by_single.setdefault(counterpart.single_tool, counterpart)
            self._by_batch.setdefault(counterpart.batch_tool, []).append(counterpart)
        self._admissible = batch_admissible or (lambda _counterpart, _target: True)
        # The resolver is the caller's (host) identity for one target.  It
        # must not raise; the host implementation degrades to a lexical
        # absolute path when the filesystem cannot answer.
        self._resolve = resolve_target or (lambda text: text)
        if isinstance(view_chars, bool) or not isinstance(view_chars, int) or view_chars < 0:
            raise ValueError("view_chars must be a non-negative integer")
        self._view_chars = view_chars
        # Per family, all scoped to the current window:
        #   _targets   keys of targets read singly and successfully (counted)
        #   _attempted keys of dispatched single calls that failed (a retry
        #              is never refused)
        #   _covered   keys returned by a successful batch call (a later
        #              single read of them is never counted or refused)
        #   _refusals  refusals issued in the window
        self._targets: dict[str, set[str]] = {}
        self._attempted: dict[str, set[str]] = {}
        self._covered: dict[str, set[str]] = {}
        self._refusals: dict[str, int] = {}
        self._stats = {"advisories": 0, "refusals": 0, "resets": 0}

    @property
    def active(self) -> bool:
        return bool(self._by_single)

    @property
    def view_chars(self) -> int:
        return self._view_chars

    def _key(self, value) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return target_key(self._resolve(value.strip()))

    def _exempt(self, counterpart: BatchCounterpart, key: str) -> bool:
        family = counterpart.family
        return (
            key in self._targets.get(family, ())
            or key in self._attempted.get(family, ())
            or key in self._covered.get(family, ())
        )

    def _batch_keys(self, counterpart: BatchCounterpart, args) -> set[str]:
        raw = args.get(counterpart.batch_argument) if isinstance(args, dict) else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return set()
        if not isinstance(raw, (list, tuple)):
            return set()
        keys = set()
        for item in raw:
            key = self._key(item)
            if key is not None:
                keys.add(key)
        return keys

    def before_dispatch(self, tool: str, args) -> BatchRefusal | None:
        """Return a refusal when this single call must not be dispatched."""
        counterpart = self._by_single.get(tool)
        if counterpart is None:
            return None
        raw = args.get(counterpart.target_argument) if isinstance(args, dict) else None
        key = self._key(raw)
        if key is None or self._exempt(counterpart, key):
            return None
        window = self._targets.get(counterpart.family, ())
        if len(window) < self.config.refuse_after:
            return None
        if not self._admissible(counterpart, raw.strip()):
            return None
        refusals = self._refusals.get(counterpart.family, 0) + 1
        self._refusals[counterpart.family] = refusals
        self._stats["refusals"] += 1
        return BatchRefusal(
            counterpart=counterpart,
            target=raw.strip(),
            distinct_targets=len(window),
            refuse_after=self.config.refuse_after,
            refusals=refusals,
            max_refusals=self.config.max_refusals,
            view_chars=self._view_chars,
        )

    def after_dispatch(self, tool: str, args, *, ok: bool) -> BatchAdvisory | None:
        """Account for a dispatched call; return an advisory when one is due.

        Only successful calls count toward a window: a failed single read did
        no wasteful work that a batch would have saved, and failure streaks
        already have their own no-progress guard.  A failed single target is
        remembered as attempted so that its retry is never refused.
        """
        if ok:
            for batch_counterpart in self._by_batch.get(tool, ()):
                family = batch_counterpart.family
                # The model complied: start a new window for the family and
                # remember what the batch returned.
                self._targets.pop(family, None)
                self._refusals.pop(family, None)
                self._covered.setdefault(family, set()).update(
                    self._batch_keys(batch_counterpart, args)
                )
        counterpart = self._by_single.get(tool)
        if counterpart is None:
            return None
        raw = args.get(counterpart.target_argument) if isinstance(args, dict) else None
        key = self._key(raw)
        if key is None:
            return None
        if not ok:
            if not self._exempt(counterpart, key):
                self._attempted.setdefault(counterpart.family, set()).add(key)
            return None
        if self._exempt(counterpart, key):
            return None
        window = self._targets.setdefault(counterpart.family, set())
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
            view_chars=self._view_chars,
        )

    def reset(self) -> None:
        """Start new windows for every family (after a state-changing step).

        Counted, attempted and covered targets and the refusal counts all
        belong to the window, so all of them restart.
        """
        if self._targets or self._attempted or self._covered or self._refusals:
            self._stats["resets"] += 1
        self._targets.clear()
        self._attempted.clear()
        self._covered.clear()
        self._refusals.clear()

    def snapshot(self) -> dict:
        """Bounded telemetry for status surfaces and tests."""
        return {
            "guard": GUARD_NAME,
            "families": {family: len(keys) for family, keys in self._targets.items()},
            "window_refusals": dict(self._refusals),
            "advisories": self._stats["advisories"],
            "refusals": self._stats["refusals"],
            "resets": self._stats["resets"],
            "advisory_after": self.config.advisory_after,
            "refuse_after": self.config.refuse_after,
            "max_refusals": self.config.max_refusals,
            "view_chars": self._view_chars,
        }
