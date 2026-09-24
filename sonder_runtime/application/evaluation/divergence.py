"""Earliest meaningful divergence and minimized, reproducible replay failures.

``trajectory_replay`` reports every raw step difference.  This module answers
the two EVAL-006 questions on top of it:

* *Where did the candidate first make a different decision?*  A
  :class:`DivergencePolicy` projects each recorded step onto its decision
  content -- optionally keeping only named ``decision_paths`` and always
  dropping ``ignored_paths`` such as latencies, timestamps, or request IDs --
  so incidental noise is not reported as a behavioral divergence.
* *What is the smallest replay that still reproduces it?*
  :func:`minimize_failure` truncates to the reproducing prefix and, when the
  caller supplies the baseline behavior, runs differential delta debugging
  (ddmin) over the remaining steps.  Every trial replays through **fresh**
  evaluators from the caller's factories, so stateful sessions restart
  cleanly rather than leaking state between trials.  The minimized replay is
  then re-run to prove it reproduces deterministically, and is returned as an
  immutable, digest-verified :class:`MinimizedFailure` that can be retained
  (see ``adapters/evaluation_failure_corpus.py``) and replayed later.

Nothing here executes a model, performs I/O, or reads a clock.  The evaluator
is supplied by the caller; tests use deterministic fakes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Mapping, Protocol, Sequence

from .trajectory_replay import TrajectoryRecord, TrajectoryStep


SCHEMA = "sonder.evaluation-minimized-failure.v1"
POLICY_SCHEMA = "sonder.evaluation-divergence-policy.v1"
# Only ``output`` is a decision field.  Replay feeds the recorded input to the
# candidate and records what it returns; step ``state`` is carried over from the
# recording, so a state comparison could never observe a candidate difference.
DECISION_FIELDS = frozenset({"output"})
MAX_PATHS = 64
MAX_PATH_DEPTH = 16
MAX_CHANGED_PATHS = 32
MAX_EVALUATIONS = 1_024
DEFAULT_MAX_EVALUATIONS = 256
STRATEGY_PREFIX = "prefix"
STRATEGY_DIFFERENTIAL = "differential_ddmin"
STRATEGIES = frozenset({STRATEGY_PREFIX, STRATEGY_DIFFERENTIAL})
MAX_RETAINED_FAILURES = 1_024
_MISSING = object()

EvaluatorFactory = Callable[[], Callable[[Any], Any]]


class DivergenceError(ValueError):
    """Invalid policy, non-reproducible failure, or inconsistent failure record."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DivergenceError("divergence values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _path(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise DivergenceError("divergence paths must be non-empty dotted strings")
    parts = tuple(raw.strip().split("."))
    if any(not part for part in parts) or len(parts) > MAX_PATH_DEPTH:
        raise DivergenceError(f"divergence path {raw!r} is malformed or too deep")
    return parts


def _drop(value: Any, path: tuple[str, ...]) -> Any:
    """Remove ``path`` from every mapping it names, through nested lists."""
    if isinstance(value, list):
        return [_drop(item, path) for item in value]
    if not isinstance(value, dict):
        return value
    head, rest = path[0], path[1:]
    if head not in value:
        return value
    copied = dict(value)
    if rest:
        copied[head] = _drop(copied[head], rest)
    else:
        del copied[head]
    return copied


def _extract(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _changed_paths(expected: Any, actual: Any, prefix: str = "") -> list[str]:
    """Return bounded dotted paths at which two JSON values first differ."""
    if _canonical(expected) == _canonical(actual):
        return []
    if isinstance(expected, dict) and isinstance(actual, dict):
        changed: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in expected or key not in actual:
                changed.append(child)
            else:
                changed.extend(_changed_paths(expected[key], actual[key], child))
            if len(changed) >= MAX_CHANGED_PATHS:
                break
        return changed[:MAX_CHANGED_PATHS]
    return [prefix or "$"]


@dataclass(frozen=True)
class DivergencePolicy:
    """Which parts of a step are decisions, and which are incidental noise."""

    fields: tuple[str, ...] = ("output",)
    decision_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    schema: str = POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != POLICY_SCHEMA:
            raise DivergenceError("unsupported divergence policy schema")
        if not isinstance(self.fields, tuple) or not self.fields or any(item not in DECISION_FIELDS for item in self.fields):
            raise DivergenceError(f"policy fields must be a non-empty subset of {sorted(DECISION_FIELDS)}")
        if len(set(self.fields)) != len(self.fields):
            raise DivergenceError("policy fields must be unique")
        for label in ("decision_paths", "ignored_paths"):
            values = getattr(self, label)
            if not isinstance(values, tuple) or len(values) > MAX_PATHS:
                raise DivergenceError(f"policy {label} must be a tuple of at most {MAX_PATHS} paths")
            for item in values:
                _path(item)
            if len(set(values)) != len(values):
                raise DivergenceError(f"policy {label} must be unique")

    def project(self, value: Any) -> Any:
        """Return the decision content of one step field."""
        _canonical(value)
        for raw in self.ignored_paths:
            value = _drop(value, _path(raw))
        if not self.decision_paths:
            return value
        projected: dict[str, Any] = {}
        for raw in self.decision_paths:
            extracted = _extract(value, _path(raw))
            projected[raw] = {"present": False} if extracted is _MISSING else {"present": True, "value": extracted}
        return projected

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "fields": list(self.fields),
            "decision_paths": list(self.decision_paths),
            "ignored_paths": list(self.ignored_paths),
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DivergencePolicy":
        if not isinstance(payload, Mapping) or set(payload) != {"schema", "fields", "decision_paths", "ignored_paths"}:
            raise DivergenceError("divergence policy payload fields are unsupported or missing")
        try:
            return cls(
                tuple(payload["fields"]), tuple(payload["decision_paths"]),
                tuple(payload["ignored_paths"]), payload["schema"],
            )
        except TypeError as exc:
            raise DivergenceError("divergence policy payload is malformed") from exc


@dataclass(frozen=True)
class MeaningfulDivergence:
    """The first step at which the projected decision content differs."""

    index: int
    field: str
    expected_digest: str
    actual_digest: str
    changed_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "field": self.field,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "changed_paths": list(self.changed_paths),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MeaningfulDivergence":
        if not isinstance(payload, Mapping) or set(payload) != {"index", "field", "expected_digest", "actual_digest", "changed_paths"}:
            raise DivergenceError("divergence payload fields are unsupported or missing")
        index = payload["index"]
        if type(index) is not int or index < 0:
            raise DivergenceError("divergence index must be a non-negative integer")
        if payload["field"] not in DECISION_FIELDS | {"step_count"}:
            raise DivergenceError("divergence field is unsupported")
        return cls(index, payload["field"], str(payload["expected_digest"]), str(payload["actual_digest"]), tuple(payload["changed_paths"]))


def earliest_divergence(
    expected: TrajectoryRecord,
    actual: TrajectoryRecord,
    policy: DivergencePolicy | None = None,
) -> MeaningfulDivergence | None:
    """Return the earliest decision divergence between two trajectories.

    Steps are compared in order; within a step, fields are compared in the
    policy's order.  A shorter or longer candidate diverges at the first
    index that exists in only one trajectory.  ``None`` means the two runs
    made the same decisions under ``policy``.
    """
    policy = policy or DivergencePolicy()
    for left, right in zip(expected.steps, actual.steps):
        for field in policy.fields:
            left_value = policy.project(getattr(left, field))
            right_value = policy.project(getattr(right, field))
            if _canonical(left_value) != _canonical(right_value):
                return MeaningfulDivergence(
                    left.index, field, _digest(left_value), _digest(right_value),
                    tuple(_changed_paths(left_value, right_value)),
                )
    if len(expected.steps) != len(actual.steps):
        index = min(len(expected.steps), len(actual.steps))
        return MeaningfulDivergence(index, "step_count", _digest(len(expected.steps)), _digest(len(actual.steps)), ())
    return None


def replay_steps(
    steps: Sequence[TrajectoryStep],
    evaluator: Callable[[Any], Any],
    *,
    trajectory_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[TrajectoryRecord, TrajectoryRecord]:
    """Replay ``steps`` (re-indexed from zero) and return (expected, actual)."""
    expected_steps: list[TrajectoryStep] = []
    actual_steps: list[TrajectoryStep] = []
    for position, step in enumerate(steps):
        expected_steps.append(TrajectoryStep(position, step.input, step.output, step.state))
        actual_steps.append(TrajectoryStep(position, step.input, evaluator(step.input), step.state))
    return (
        TrajectoryRecord.from_steps(trajectory_id, expected_steps, metadata=metadata),
        TrajectoryRecord.from_steps(trajectory_id, actual_steps, metadata=metadata),
    )


def replay_divergence(
    expected: TrajectoryRecord,
    evaluator_factory: EvaluatorFactory,
    policy: DivergencePolicy | None = None,
) -> MeaningfulDivergence | None:
    """Replay the whole trajectory through a fresh evaluator and locate divergence."""
    recorded, actual = replay_steps(
        expected.steps, evaluator_factory(), trajectory_id=expected.trajectory_id, metadata=expected.metadata,
    )
    return earliest_divergence(recorded, actual, policy)


@dataclass(frozen=True)
class MinimizedFailure:
    """Immutable minimal replay that reproduces one recorded divergence.

    The digests are integrity checks against accidental corruption and
    inconsistent edits, not tamper-proofing: they are plain SHA-256 values that
    anyone able to edit a record can recompute.  Construction and loading
    therefore also re-derive what they can from the stored steps -- the
    divergent step must be the last retained step and its projected expected
    content must match the recorded divergence digest.
    """

    source_trajectory_id: str
    source_digest: str
    policy: DivergencePolicy
    source_indexes: tuple[int, ...]
    steps: tuple[TrajectoryStep, ...]
    divergence: MeaningfulDivergence
    reproduction_digest: str
    evaluations: int
    one_minimal: bool
    strategy: str
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise DivergenceError("unsupported minimized failure schema")
        if not self.source_trajectory_id or not isinstance(self.source_trajectory_id, str):
            raise DivergenceError("source_trajectory_id is required")
        if not self.steps or len(self.steps) != len(self.source_indexes):
            raise DivergenceError("minimized failure steps must align with source indexes")
        if tuple(sorted(set(self.source_indexes))) != self.source_indexes or any(
            type(item) is not int or item < 0 for item in self.source_indexes
        ):
            raise DivergenceError("source indexes must be unique, sorted, non-negative integers")
        if tuple(step.index for step in self.steps) != tuple(range(len(self.steps))):
            raise DivergenceError("minimized failure steps must be contiguous from zero")
        if self.divergence.field not in self.policy.fields or self.divergence.index != len(self.steps) - 1:
            raise DivergenceError("divergence must be on a policy decision field at the last retained step")
        divergent = getattr(self.steps[self.divergence.index], self.divergence.field)
        if _digest(self.policy.project(divergent)) != self.divergence.expected_digest:
            raise DivergenceError("divergence expected digest does not match the stored step")
        if type(self.evaluations) is not int or self.evaluations < 1 or type(self.one_minimal) is not bool:
            raise DivergenceError("minimization bookkeeping is invalid")
        if self.strategy not in STRATEGIES:
            raise DivergenceError(f"minimization strategy must be one of {sorted(STRATEGIES)}")
        if self.one_minimal and self.strategy != STRATEGY_DIFFERENTIAL:
            raise DivergenceError("only differential minimization can claim one-minimality")

    @property
    def trajectory(self) -> TrajectoryRecord:
        """The minimized replay as an ordinary recorded trajectory."""
        return TrajectoryRecord.from_steps(
            f"{self.source_trajectory_id}:minimized",
            self.steps,
            metadata={"source_digest": self.source_digest, "source_indexes": list(self.source_indexes)},
        )

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": self.schema,
            "source_trajectory_id": self.source_trajectory_id,
            "source_digest": self.source_digest,
            "policy": self.policy.as_dict(),
            "source_indexes": list(self.source_indexes),
            "trajectory": self.trajectory.as_dict(),
            "divergence": self.divergence.as_dict(),
            "reproduction_digest": self.reproduction_digest,
            "evaluations": self.evaluations,
            "one_minimal": self.one_minimal,
            "strategy": self.strategy,
        }
        if include_digest:
            value["failure_digest"] = self.digest
        return value

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MinimizedFailure":
        """Restore a retained failure, re-checking digests and step consistency."""
        fields = {
            "schema", "source_trajectory_id", "source_digest", "policy", "source_indexes",
            "trajectory", "divergence", "reproduction_digest", "evaluations", "one_minimal",
            "strategy", "failure_digest",
        }
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise DivergenceError("minimized failure payload fields are unsupported or missing")
        try:
            trajectory = TrajectoryRecord.from_dict(payload["trajectory"])
            record = cls(
                str(payload["source_trajectory_id"]), str(payload["source_digest"]),
                DivergencePolicy.from_dict(payload["policy"]), tuple(payload["source_indexes"]),
                trajectory.steps, MeaningfulDivergence.from_dict(payload["divergence"]),
                str(payload["reproduction_digest"]), payload["evaluations"], payload["one_minimal"],
                payload["strategy"], payload["schema"],
            )
        except DivergenceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise DivergenceError("minimized failure payload is malformed") from exc
        if trajectory.digest != record.trajectory.digest:
            raise DivergenceError("minimized failure trajectory identity mismatch")
        if payload["failure_digest"] != record.digest:
            raise DivergenceError("minimized failure digest mismatch")
        return record


def _ddmin(
    items: tuple[int, ...],
    fails: Callable[[tuple[int, ...]], bool],
    budget: Callable[[], bool],
) -> tuple[tuple[int, ...], bool]:
    """Zeller's ddmin; returns (subset, completed_without_exhausting_budget)."""
    granularity = 2
    while len(items) >= 2:
        if budget():
            return items, False
        size = -(-len(items) // granularity)
        chunks = [items[start:start + size] for start in range(0, len(items), size)]
        reduced = False
        for chunk in chunks:
            if budget():
                return items, False
            if fails(chunk):
                items, granularity, reduced = chunk, 2, True
                break
        if not reduced:
            for position in range(len(chunks)):
                if budget():
                    return items, False
                complement = tuple(item for other, chunk in enumerate(chunks) if other != position for item in chunk)
                if fails(complement):
                    items, granularity, reduced = complement, max(granularity - 1, 2), True
                    break
        if not reduced:
            if granularity >= len(items):
                return items, True
            granularity = min(granularity * 2, len(items))
    return items, True


def minimize_failure(
    expected: TrajectoryRecord,
    evaluator_factory: EvaluatorFactory,
    policy: DivergencePolicy | None = None,
    *,
    baseline_factory: EvaluatorFactory | None = None,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
) -> MinimizedFailure:
    """Reduce a divergent replay to a small step set reproducing the same divergence.

    Recorded outputs are only a valid oracle for the exact sequence that
    produced them: once an earlier step is removed, a later recorded output
    may depend on state that no longer exists.  Two sound strategies follow:

    * Without ``baseline_factory`` the result is the shortest reproducing
      **prefix** -- the steps through the earliest meaningful divergence,
      whose recorded outputs remain valid because nothing before them moved.
    * With ``baseline_factory`` (the behavior the recording came from), the
      oracle is **differential**: each trial replays a subset through a fresh
      baseline and a fresh candidate.  The baseline must first reproduce the
      full recording under ``policy``, and the retained expected outputs are
      the baseline's outputs for the minimized subset.

    Minimization is anchored to the originally reported divergence: the
    divergent step is always retained as the last step, and a trial counts
    only if it diverges *first* at that step with the same projected expected
    and actual content.  A subset that fails some other way (a different bug
    exposed by removing context) is not accepted as a reproduction.

    ``max_evaluations`` is a hard ceiling on replays, including the baseline
    faithfulness check (1), the initial full replay (1), and the two
    confirmation replays (2).  It must leave room for those fixed replays;
    whatever remains is the ddmin budget.  ``one_minimal`` is true only when
    differential ddmin converged within that budget.
    """
    policy = policy or DivergencePolicy()
    reserved = 3 + (1 if baseline_factory is not None else 0)
    if type(max_evaluations) is not int or not reserved <= max_evaluations <= MAX_EVALUATIONS:
        raise DivergenceError(f"max_evaluations must be within {reserved}..{MAX_EVALUATIONS} for this strategy")
    evaluations = 0

    def replay(indexes: tuple[int, ...]) -> tuple[TrajectoryRecord, MeaningfulDivergence | None, str]:
        """Return (reference, divergence, candidate digest) for one subset."""
        nonlocal evaluations
        evaluations += 1
        chosen = [expected.steps[index] for index in indexes]
        if baseline_factory is None:
            reference, _ = replay_steps(
                chosen, lambda value: None, trajectory_id=expected.trajectory_id, metadata=expected.metadata,
            )
        else:
            _, reference = replay_steps(
                chosen, baseline_factory(), trajectory_id=expected.trajectory_id, metadata=expected.metadata,
            )
        _, actual = replay_steps(
            chosen, evaluator_factory(), trajectory_id=expected.trajectory_id, metadata=expected.metadata,
        )
        return reference, earliest_divergence(reference, actual, policy), actual.digest

    full = tuple(range(len(expected.steps)))
    if not full:
        raise DivergenceError("an empty trajectory cannot diverge")
    if baseline_factory is not None:
        evaluations += 1
        recorded, _ = replay_steps(
            expected.steps, lambda value: None, trajectory_id=expected.trajectory_id, metadata=expected.metadata,
        )
        _, rebuilt = replay_steps(
            expected.steps, baseline_factory(), trajectory_id=expected.trajectory_id, metadata=expected.metadata,
        )
        if earliest_divergence(recorded, rebuilt, policy) is not None:
            raise DivergenceError("baseline does not reproduce the recorded trajectory")
    _, first, _ = replay(full)
    if first is None:
        raise DivergenceError("replay does not diverge under the supplied policy")
    target = first.index
    context = full[:target]

    def same_divergence(found: MeaningfulDivergence | None, position: int) -> bool:
        return (
            found is not None and found.index == position and found.field == first.field
            and found.expected_digest == first.expected_digest
            and found.actual_digest == first.actual_digest
        )

    converged = False
    minimized = context
    if baseline_factory is not None:
        ddmin_budget = max_evaluations - 2  # the two confirmation replays stay reserved
        cache: dict[tuple[int, ...], bool] = {}

        def fails(indexes: tuple[int, ...]) -> bool:
            if indexes not in cache:
                cache[indexes] = same_divergence(replay(indexes + (target,))[1], len(indexes))
            return cache[indexes]

        def exhausted() -> bool:
            return evaluations >= ddmin_budget

        if not exhausted() and fails(()):
            minimized, converged = (), True
        elif not exhausted():
            minimized, converged = _ddmin(context, fails, exhausted)

    chosen = minimized + (target,)
    confirmations = [replay(chosen) for _ in range(2)]
    divergences = [item[1] for item in confirmations]
    references = {item[0].digest for item in confirmations}
    digests = {item[2] for item in confirmations}
    if (
        not all(same_divergence(item, len(minimized)) for item in divergences)
        or divergences[0] != divergences[1] or len(digests) != 1 or len(references) != 1
    ):
        raise DivergenceError("minimized failure does not reproduce deterministically")
    reference = confirmations[0][0]
    steps = tuple(
        TrajectoryStep(position, step.input, step.output, step.state)
        for position, step in enumerate(reference.steps)
    )
    return MinimizedFailure(
        expected.trajectory_id, expected.digest, policy, chosen, steps,
        divergences[0], digests.pop(), evaluations, converged,
        STRATEGY_DIFFERENTIAL if baseline_factory is not None else STRATEGY_PREFIX,
    )


def reproduce(failure: MinimizedFailure, evaluator_factory: EvaluatorFactory) -> MeaningfulDivergence | None:
    """Replay a retained failure; ``None`` means the candidate no longer diverges."""
    recorded, actual = replay_steps(
        failure.steps, evaluator_factory(), trajectory_id=failure.source_trajectory_id,
    )
    return earliest_divergence(recorded, actual, failure.policy)


class MinimizedFailureStore(Protocol):
    """Retain minimized failures by content digest."""

    def retain(self, failure: MinimizedFailure) -> str: ...

    def load(self, failure_digest: str) -> MinimizedFailure: ...

    def digests(self) -> tuple[str, ...]: ...


class InMemoryMinimizedFailureStore:
    """Bounded, idempotent reference store keyed by failure digest."""

    def __init__(self, *, max_failures: int = MAX_RETAINED_FAILURES) -> None:
        if type(max_failures) is not int or not 1 <= max_failures <= MAX_RETAINED_FAILURES:
            raise DivergenceError(f"max_failures must be within 1..{MAX_RETAINED_FAILURES}")
        self._max = max_failures
        self._failures: dict[str, dict[str, Any]] = {}

    def retain(self, failure: MinimizedFailure) -> str:
        digest = failure.digest
        if digest not in self._failures and len(self._failures) >= self._max:
            raise DivergenceError("minimized failure store is full")
        self._failures[digest] = failure.as_dict()
        return digest

    def load(self, failure_digest: str) -> MinimizedFailure:
        try:
            payload = self._failures[failure_digest]
        except KeyError as exc:
            raise DivergenceError(f"unknown minimized failure {failure_digest!r}") from exc
        return MinimizedFailure.from_dict(payload)

    def digests(self) -> tuple[str, ...]:
        return tuple(sorted(self._failures))


__all__ = [
    "STRATEGY_DIFFERENTIAL", "STRATEGY_PREFIX",
    "DivergenceError", "DivergencePolicy", "EvaluatorFactory", "InMemoryMinimizedFailureStore",
    "MeaningfulDivergence", "MinimizedFailure", "MinimizedFailureStore", "earliest_divergence",
    "minimize_failure", "replay_divergence", "replay_steps", "reproduce",
]
