"""Pure adaptive-concurrency and ownership-aware lane admission policy.

Issue #510 section 3 ("bounded productive parallelism") asks for useful
independent lanes rather than maximum live-worker count: concurrency caps,
ownership-aware scheduling, aggressive delegation for independent work, and an
automatic reduction of concurrency during churn, retry storms, resource
pressure, or tight coupling.

This module is the deterministic policy half of that behaviour.  It performs
no I/O, reads no clock, and owns no threading primitive; the caller supplies
every observation and owns synchronisation.  Two independent pieces:

* **Ownership.**  A :class:`LaneClaim` names the paths a lane reads or writes.
  Two claims conflict when their paths overlap (equal, or one is an ancestor
  of the other) and at least one side writes.  :func:`admissible_lanes` picks,
  in stable input order, the pending lanes that may start now without
  exceeding the cap or overlapping a running/admitted conflicting lane --
  conflicting lanes are serialized, independent lanes run in parallel.
  Readers never conflict with readers, so read-only fan-out is not throttled.

* **Adaptive cap.**  :func:`observe` folds one worker outcome plus an optional
  resource snapshot into a :class:`ConcurrencyState` and returns the next
  state and a :class:`ConcurrencyDecision`.  A retry storm, churn burst, or
  resource pressure shrinks the cap multiplicatively (fast); only a sustained
  healthy streak with pressure released grows it additively (slow).  Pressure
  uses separate enter/exit thresholds so the cap does not oscillate at a
  boundary, and consumed evidence is cleared after a shrink so one burst is
  never counted twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Collection, Mapping, Sequence


# Pressure bands mirror ``sonder_runtime.domain.fleet_pressure``.  They are
# repeated as plain strings so this policy stays independent of the tracker.
_HIGH_PRESSURE_BANDS = frozenset({"high", "critical"})
_RELEASED_PRESSURE_BANDS = frozenset({"low", "medium"})


class LaneAccess(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class LaneClaim:
    """The paths one lane touches and whether it may mutate them.

    An empty ``paths`` set means the lane owns nothing on disk (for example a
    pure design question) and is independent of every other lane.  A claim of
    ``"."`` covers the whole scope root.
    """

    lane_id: str
    paths: frozenset[str] = frozenset()
    access: LaneAccess = LaneAccess.READ

    def __post_init__(self) -> None:
        if not isinstance(self.lane_id, str) or not self.lane_id.strip():
            raise ValueError("lane_id is required")
        if not isinstance(self.access, LaneAccess):
            raise ValueError("access must be a LaneAccess")
        object.__setattr__(
            self, "paths", frozenset(_normalize_path(path) for path in self.paths),
        )


def _path_parts(path: str) -> tuple[bool, tuple[str, ...]]:
    """Return ``(absolute, parts)`` with case folded and separators unified.

    Case is folded on purpose: on a case-insensitive filesystem ``A.py`` and
    ``a.py`` are the same file, and a false conflict only costs parallelism
    while a missed one can corrupt a shared file.
    """
    if not isinstance(path, str):
        raise ValueError("claimed paths must be strings")
    text = path.replace("\\", "/").strip().casefold()
    if not text:
        raise ValueError("claimed paths must be non-empty")
    absolute = text.startswith("/") or (
        len(text) >= 2 and text[1] == ":" and text[0].isalpha()
    )
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".", "/")]
    if ".." in parts:
        raise ValueError("claimed paths must not escape the scope root")
    return absolute, tuple(parts)


def _normalize_path(path: str) -> str:
    """Return a comparable root-relative ``a/b/c`` path; ``""`` is the root.

    Absolute paths are rejected: two spellings of one file (``a.py`` and
    ``C:/repo/a.py``) must be anchored with :func:`anchor_claim_path` first,
    otherwise they would silently compare as different files.
    """
    absolute, parts = _path_parts(path)
    if absolute:
        raise ValueError("claimed paths must be anchored to the scope root")
    return "/".join(parts)


def anchor_claim_path(path: str, root: str) -> str:
    """Express ``path`` relative to the scope ``root``.

    A relative path is already root-relative.  An absolute path must lie
    inside ``root``; anything else raises ``ValueError`` so an unanchorable
    claim can never masquerade as an independent lane.
    """
    absolute, parts = _path_parts(path)
    if not absolute:
        return "/".join(parts)
    if not isinstance(root, str) or not root.strip():
        raise ValueError("an absolute claim needs a scope root to anchor to")
    root_absolute, root_parts = _path_parts(root)
    if not root_absolute:
        raise ValueError("the scope root must be absolute")
    if parts[:len(root_parts)] != root_parts:
        raise ValueError("claimed path lies outside the scope root")
    return "/".join(parts[len(root_parts):])


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(left.split("/")) if left else ()
    right_parts = tuple(right.split("/")) if right else ()
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def claims_conflict(left: LaneClaim, right: LaneClaim) -> bool:
    """True when the two lanes must not run at the same time."""
    if left.lane_id == right.lane_id:
        return False
    if left.access is LaneAccess.READ and right.access is LaneAccess.READ:
        return False
    return any(
        _paths_overlap(a, b) for a in left.paths for b in right.paths
    )


def conflict_graph(claims: Sequence[LaneClaim]) -> dict[str, frozenset[str]]:
    """Map each lane to the set of lanes it conflicts with."""
    ids = [claim.lane_id for claim in claims]
    if len(set(ids)) != len(ids):
        raise ValueError("lane ids must be unique")
    graph: dict[str, set[str]] = {lane_id: set() for lane_id in ids}
    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            if claims_conflict(left, right):
                graph[left.lane_id].add(right.lane_id)
                graph[right.lane_id].add(left.lane_id)
    return {lane_id: frozenset(peers) for lane_id, peers in graph.items()}


def coupled_lane_count(graph: Mapping[str, Collection[str]]) -> int:
    """Number of lanes that share ownership with at least one other lane."""
    return sum(1 for peers in graph.values() if peers)


def admissible_lanes(
    pending: Sequence[str],
    running: Collection[str],
    cap: int,
    graph: Mapping[str, Collection[str]],
) -> tuple[str, ...]:
    """Pending lanes that may start now, in stable input order.

    A lane is admitted while the running + admitted count stays below ``cap``
    and it conflicts with no running or already-admitted lane.  A blocked lane
    does not block later independent lanes (no head-of-line blocking), but it
    keeps its place in ``pending`` for the next call.
    """
    if cap < 1:
        raise ValueError("cap must be at least one")
    occupied = set(running)
    admitted: list[str] = []
    for lane_id in pending:
        if len(occupied) >= cap:
            break
        if lane_id in occupied:
            continue
        peers = graph.get(lane_id, ())
        if any(peer in occupied for peer in peers):
            continue
        admitted.append(lane_id)
        occupied.add(lane_id)
    return tuple(admitted)


class Outcome(str, Enum):
    """Content-free worker outcome vocabulary fed to :func:`observe`."""

    SUCCEEDED = "succeeded"
    # One transient transport/availability/throttle failure, whether it was
    # retried or exhausted the retry budget.  Many of these in a short window
    # is a retry storm.
    TRANSIENT_RETRY = "transient_retry"
    # A permanent worker failure.  Neutral: it neither grows nor shrinks.
    FAILED = "failed"
    # The lane's owned target changed underneath it (or the lane had to be
    # restarted because of such a change).  Repeated churn means lanes or an
    # outside writer are fighting over the same files.
    CHURN = "churn"
    # Resource sample only; no worker finished.
    SAMPLE = "sample"


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Caller-measured resource signal; either field may be unknown."""

    memory_available_fraction: float | None = None
    pressure_band: str | None = None


@dataclass(frozen=True, slots=True)
class ConcurrencyPolicy:
    ceiling: int
    floor: int = 1
    # Sliding window of recent outcomes used for storm/churn detection.
    window: int = 8
    retry_storm_threshold: int = 3
    churn_threshold: int = 2
    # Resource pressure enters below ``enter`` and releases only at/above
    # ``exit``; the gap is the hysteresis band.
    memory_pressure_enter: float = 0.10
    memory_pressure_exit: float = 0.20
    # Multiplicative decrease, additive increase.
    shrink_divisor: int = 2
    grow_step: int = 1
    # Consecutive successes (with pressure released) required before growth.
    recovery_streak: int = 3
    # Observations that must pass after a shrink before pressure may shrink
    # again, so a persistent pressure signal walks the cap down gradually.
    pressure_cooldown: int = 2
    # Consecutive *unknown* resource readings (the probe answered but knew
    # nothing) after which latched pressure is released.  Without this a
    # probe that goes dark while pressured would pin the cap forever.
    unknown_release_after: int = 3

    def __post_init__(self) -> None:
        if self.floor < 1:
            raise ValueError("floor must be at least one")
        if self.ceiling < self.floor:
            raise ValueError("ceiling must be at least floor")
        if self.window < 1:
            raise ValueError("window must be positive")
        if self.retry_storm_threshold < 1 or self.churn_threshold < 1:
            raise ValueError("trigger thresholds must be positive")
        if self.retry_storm_threshold > self.window or self.churn_threshold > self.window:
            raise ValueError("trigger thresholds must fit inside the window")
        if not 0.0 <= self.memory_pressure_enter < self.memory_pressure_exit <= 1.0:
            raise ValueError("memory pressure thresholds need enter < exit within [0, 1]")
        if self.shrink_divisor < 2:
            raise ValueError("shrink_divisor must be at least two")
        if self.grow_step < 1 or self.recovery_streak < 1 or self.pressure_cooldown < 0:
            raise ValueError("growth and cooldown settings must be positive")
        if self.unknown_release_after < 1:
            raise ValueError("unknown_release_after must be positive")


@dataclass(frozen=True, slots=True)
class ConcurrencyState:
    cap: int
    # (outcome, source lane or None) pairs, newest last.
    recent: tuple[tuple[Outcome, str | None], ...] = ()
    healthy_streak: int = 0
    pressured: bool = False
    since_shrink: int = 0
    observations: int = 0
    unknown_streak: int = 0


@dataclass(frozen=True, slots=True)
class ConcurrencyDecision:
    previous_cap: int
    cap: int
    action: str  # "shrink" | "grow" | "hold"
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "previous_cap": self.previous_cap,
            "cap": self.cap,
            "action": self.action,
            "reasons": list(self.reasons),
        }


REASON_RETRY_STORM = "retry_storm"
REASON_CHURN = "churn"
REASON_RESOURCE_PRESSURE = "resource_pressure"
REASON_RECOVERED = "recovered"


def initial_state(policy: ConcurrencyPolicy, cap: int | None = None) -> ConcurrencyState:
    start = policy.ceiling if cap is None else cap
    return ConcurrencyState(cap=max(policy.floor, min(policy.ceiling, int(start))))


def _resource_pressure(
    snapshot: ResourceSnapshot | None,
    currently: bool,
    unknown_streak: int,
    policy: ConcurrencyPolicy,
) -> tuple[bool, bool, int]:
    """Return ``(pressured, breaching, unknown_streak)``.

    ``breaching`` means a known signal is past its enter threshold right now.
    ``pressured`` is the hysteresis state: it turns on with a breach and turns
    off once every known signal is back inside its exit band.  ``None`` means
    "not sampled" and changes nothing.  A snapshot with no known field is an
    *unknown reading*: it never enters pressure, and after
    ``unknown_release_after`` consecutive unknown readings it releases latched
    pressure, so a dark probe cannot pin the cap.
    """
    if snapshot is None:
        return currently, False, unknown_streak
    fraction = snapshot.memory_available_fraction
    band = snapshot.pressure_band
    if fraction is None and band is None:
        streak = unknown_streak + 1
        if currently and streak >= policy.unknown_release_after:
            return False, False, streak
        return currently, False, streak
    breaching = (
        (fraction is not None and fraction < policy.memory_pressure_enter)
        or (band is not None and band in _HIGH_PRESSURE_BANDS)
    )
    if breaching:
        return True, True, 0
    if not currently:
        return False, False, 0
    memory_released = fraction is None or fraction >= policy.memory_pressure_exit
    band_released = band is None or band in _RELEASED_PRESSURE_BANDS
    return not (memory_released and band_released), False, 0


def _retry_storm_weight(recent) -> int:
    """Transient failures in the window, at most one per source lane.

    One flaky lane retrying several times is that lane's problem, not a
    fleet-wide storm; unattributed (``None``) reports each count once.
    """
    lanes = set()
    anonymous = 0
    for outcome, source in recent:
        if outcome is not Outcome.TRANSIENT_RETRY:
            continue
        if source is None:
            anonymous += 1
        else:
            lanes.add(source)
    return anonymous + len(lanes)


def observe(
    state: ConcurrencyState,
    outcome: Outcome,
    policy: ConcurrencyPolicy,
    resources: ResourceSnapshot | None = None,
    *,
    source: str | None = None,
) -> tuple[ConcurrencyState, ConcurrencyDecision]:
    """Fold one observation into the state and decide the next cap.

    ``source`` names the reporting lane; a retry storm counts at most one
    transient failure per lane inside the window.
    """
    if not isinstance(outcome, Outcome):
        raise ValueError("outcome must be an Outcome")
    recent = state.recent
    if outcome is not Outcome.SAMPLE:
        recent = (recent + ((outcome, source),))[-policy.window:]
    pressured, breaching, unknown_streak = _resource_pressure(
        resources, state.pressured, state.unknown_streak, policy,
    )
    since_shrink = state.since_shrink + 1

    reasons: list[str] = []
    if _retry_storm_weight(recent) >= policy.retry_storm_threshold:
        reasons.append(REASON_RETRY_STORM)
    if sum(1 for item, _ in recent if item is Outcome.CHURN) >= policy.churn_threshold:
        reasons.append(REASON_CHURN)
    # A fresh breach shrinks at once; a persisting breach shrinks again only
    # after the cooldown.  Inside the hysteresis band (pressured but no longer
    # breaching) the cap holds: it neither shrinks further nor grows.
    if breaching and (not state.pressured or since_shrink > policy.pressure_cooldown):
        reasons.append(REASON_RESOURCE_PRESSURE)

    cap = state.cap
    if reasons:
        shrunk = max(policy.floor, cap // policy.shrink_divisor)
        # Storm/churn evidence has been acted on; clear it so the same burst
        # cannot shrink the cap a second time.  Unrelated outcomes survive.
        consumed = set()
        if REASON_RETRY_STORM in reasons:
            consumed.add(Outcome.TRANSIENT_RETRY)
        if REASON_CHURN in reasons:
            consumed.add(Outcome.CHURN)
        recent = tuple(entry for entry in recent if entry[0] not in consumed)
        next_state = ConcurrencyState(
            cap=shrunk,
            recent=recent,
            healthy_streak=0,
            pressured=pressured,
            since_shrink=0,
            observations=state.observations + 1,
            unknown_streak=unknown_streak,
        )
        action = "shrink" if shrunk < cap else "hold"
        return next_state, ConcurrencyDecision(cap, shrunk, action, tuple(reasons))

    if outcome is Outcome.SUCCEEDED and not pressured:
        streak = state.healthy_streak + 1
    elif outcome in (Outcome.TRANSIENT_RETRY, Outcome.CHURN) or pressured:
        streak = 0
    else:
        streak = state.healthy_streak
    action = "hold"
    decision_reasons: tuple[str, ...] = ()
    if streak >= policy.recovery_streak and cap < policy.ceiling:
        cap = min(policy.ceiling, cap + policy.grow_step)
        streak = 0
        action = "grow"
        decision_reasons = (REASON_RECOVERED,)
    next_state = replace(
        state,
        cap=cap,
        recent=recent,
        healthy_streak=streak,
        pressured=pressured,
        since_shrink=since_shrink,
        observations=state.observations + 1,
        unknown_streak=unknown_streak,
    )
    return next_state, ConcurrencyDecision(state.cap, cap, action, decision_reasons)


__all__ = [
    "ConcurrencyDecision",
    "ConcurrencyPolicy",
    "ConcurrencyState",
    "LaneAccess",
    "LaneClaim",
    "Outcome",
    "REASON_CHURN",
    "REASON_RECOVERED",
    "REASON_RESOURCE_PRESSURE",
    "REASON_RETRY_STORM",
    "ResourceSnapshot",
    "admissible_lanes",
    "anchor_claim_path",
    "claims_conflict",
    "conflict_graph",
    "coupled_lane_count",
    "initial_state",
    "observe",
]
