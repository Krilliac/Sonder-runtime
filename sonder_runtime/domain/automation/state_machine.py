"""Pure Autopilot/fleet state machines (SPEC-3 Phase 6, section 7).

The canonical status classification and transition validity for durable
work, as pure functions. ``autopilot_store`` and ``fleet_store`` import
their status tuples from here so there is one definition; the transition
validators encode the SPEC-3 lifecycle so the application layer can reject
illegal moves before a compare-and-set, while the stores' existing
compare-and-set SQL remains the concurrency authority.

No I/O, no SQLite — behavior of the stores is unchanged (same tuples);
the transition rules are new, tested, pure capability.
"""
from __future__ import annotations

# --- Autopilot -------------------------------------------------------------

AUTOPILOT_ACTIVE = ("planning", "running")
AUTOPILOT_RESUMABLE = ("ready", "paused", "blocked", "interrupted")
AUTOPILOT_TERMINAL = ("completed", "failed", "cancelled")
AUTOPILOT_ALL = AUTOPILOT_ACTIVE + AUTOPILOT_RESUMABLE + AUTOPILOT_TERMINAL

# Allowed autopilot transitions (SPEC-3 section 7). Resume/retry out of a
# resumable or failed state is explicit; terminal states never leave.
_AUTOPILOT_TRANSITIONS: dict[str, frozenset[str]] = {
    "ready": frozenset({"planning", "running", "cancelled"}),
    "planning": frozenset({"running", "paused", "blocked", "failed",
                           "interrupted", "cancelled"}),
    "running": frozenset({"paused", "blocked", "completed", "failed",
                          "interrupted", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "blocked": frozenset({"running", "failed", "cancelled"}),
    "interrupted": frozenset({"running", "cancelled"}),  # explicit resume
    "failed": frozenset({"running"}),                    # explicit retry
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def is_terminal(status: str) -> bool:
    return status in AUTOPILOT_TERMINAL


def is_active(status: str) -> bool:
    return status in AUTOPILOT_ACTIVE


def is_resumable(status: str) -> bool:
    return status in AUTOPILOT_RESUMABLE


def autopilot_can_transition(current: str, nxt: str) -> bool:
    if current not in _AUTOPILOT_TRANSITIONS:
        return False
    return nxt in _AUTOPILOT_TRANSITIONS[current]


def autopilot_next_states(current: str) -> frozenset[str]:
    return _AUTOPILOT_TRANSITIONS.get(current, frozenset())


# --- Fleet -----------------------------------------------------------------

FLEET_ACTIVE = ("queued", "running")
FLEET_TERMINAL = (
    "done", "failed", "task_drift", "cancelled", "interrupted", "retried",
)
FLEET_ALL = FLEET_ACTIVE + FLEET_TERMINAL

# A fleet agent is claimed from queued, runs, and reaches a terminal state.
# 'retried' and 'interrupted' can be re-dispatched to 'queued' explicitly
# (mirrors fleet_store's WHERE status IN ('interrupted','failed','cancelled')
# retry path).
_FLEET_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "interrupted"}),
    "running": frozenset({
        "done", "failed", "task_drift", "cancelled", "interrupted",
    }),
    "interrupted": frozenset({"queued", "retried"}),
    "failed": frozenset({"queued", "retried"}),
    "task_drift": frozenset({"queued", "retried"}),
    "cancelled": frozenset({"queued", "retried"}),
    "done": frozenset(),
    "retried": frozenset(),
}


def fleet_is_terminal(status: str) -> bool:
    return status in FLEET_TERMINAL


def fleet_is_active(status: str) -> bool:
    return status in FLEET_ACTIVE


def fleet_can_transition(current: str, nxt: str) -> bool:
    if current not in _FLEET_TRANSITIONS:
        return False
    return nxt in _FLEET_TRANSITIONS[current]
