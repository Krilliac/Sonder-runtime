"""Attribute verification results back to the work that produced them.

The outcome store is not short of data -- it holds ~9,200 rows -- it is short of
*honest* data. 8,883 of those are ``tests_passed`` from the self-graded
curriculum. The population that actually measures delegated work, the
caller-judged one, is 101 good against 91 rejected: 192 rows, 52.6%. Averaging
the two produces a number that reads like accuracy and is not one, which is why
``record_outcome``'s own docstring keeps them apart.

The reason the honest population is thin is that recording an outcome is a
manual step performed by whoever called the tool, and people record success far
more readily than failure. Asking harder does not fix a reporting bias; removing
the human from the report does.

Sonder already runs tools that know the truth. ``test_run`` knows whether tests
passed. ``build_run`` knows whether it compiled. ``lint_run``, ``typecheck_run``
and ``run_code`` all return a verdict. When one of those runs shortly after a
generation, its result *is* the outcome of that generation -- execution-grounded
evidence, which the reward table already weights highest, obtained without
anyone remembering to file it.

This module keeps a short-lived ledger of generations awaiting judgement and
attributes the next relevant verification to them.

Deliberate limits
-----------------
* Attribution needs a *plausible* link, not a certain one, so it is bounded by
  time, by project, and to one signal per generation per verification kind.
  A wrong attribution is worse than a missing one -- it poisons the very
  population this exists to clean up -- so every rule here fails toward
  recording nothing.
* Only genuinely execution-grounded tools may attribute. A tool whose "success"
  means "it ran", not "the work was good", is not evidence.

Stdlib only.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# How long a generation stays eligible for judgement. Long enough for a build
# or test cycle, short enough that an unrelated later run cannot claim it.
ATTRIBUTION_WINDOW_SECONDS = 900.0

# Never let the ledger grow without bound in a long-lived server.
MAX_PENDING = 64

# Tools whose verdict is real evidence about generated work, and what their
# pass/fail means in the existing signal vocabulary. `compiled` (+0.70) sits
# below the good threshold of 0.71 on purpose: building is not passing.
VERIFIERS = {
    "test_run": ("tests_passed", "failed"),
    "build_run": ("compiled", "failed"),
    "typecheck_run": ("compiled", "failed"),
    "lint_run": ("compiled", "failed"),
    "run_code": ("compiled", "failed"),
    "run_project": ("compiled", "failed"),
    "isolated_run": ("compiled", "failed"),
    "codegen_build_loop": ("compiled", "failed"),
    "artifact_verify": ("accepted", "rejected"),
    "ground_artifact": ("accepted", "rejected"),
    "artifact_ground": ("accepted", "rejected"),
}

# Tools that produce work worth judging later.
GENERATORS = frozenset({
    "sonder", "offload", "agent", "workbench_agent", "improve_function",
    "codegen_build_loop", "artifact_generate", "scaffold_project",
    "game_generate_and_test", "ensemble_answer", "consult",
    "apply_learned", "file_write", "file_batch_write", "file_edit",
    "text_patch", "json_patch", "apply_patch", "rename_symbol",
})

_LOCK = threading.RLock()


@dataclass
class _Pending:
    interaction_id: str
    tool: str
    project: str
    created: float
    run_id: str = ""
    judged: set = field(default_factory=set)   # verification kinds already applied


_PENDING: list[_Pending] = []
_STATS = {"noted": 0, "attributed": 0, "expired": 0, "unlinked": 0}


def _now() -> float:
    return time.monotonic()


def _prune(now: float | None = None) -> None:
    now = _now() if now is None else now
    with _LOCK:
        before = len(_PENDING)
        _PENDING[:] = [
            p for p in _PENDING
            if now - p.created <= ATTRIBUTION_WINDOW_SECONDS
        ]
        _STATS["expired"] += before - len(_PENDING)
        if len(_PENDING) > MAX_PENDING:
            dropped = len(_PENDING) - MAX_PENDING
            del _PENDING[:dropped]
            _STATS["expired"] += dropped


def note_generation(interaction_id: str, tool: str, project: str = "", run_id: str = "") -> bool:
    """Record that `tool` produced work that a later verification can judge.

    Returns False when there is nothing judgeable -- no interaction id means no
    row to attach an outcome to.

    `run_id` is the caller's run/span identity (e.g. an agent loop's
    activity_tracker response id) when one is available. It is a stronger
    link than project + time window alone: two concurrent runs (autopilot
    spawns one thread per run) can share a project -- or both leave it blank
    -- and without a run identity the newest pending generation from either
    run looks like a valid candidate for the other's verification.
    """
    ident = str(interaction_id or "").strip()
    name = str(tool or "").strip().lstrip("/")
    if not ident or name not in GENERATORS:
        return False
    _prune()
    with _LOCK:
        # A repeat generation for the same interaction replaces the old entry
        # rather than stacking, so one id cannot collect several verdicts.
        _PENDING[:] = [p for p in _PENDING if p.interaction_id != ident]
        _PENDING.append(
            _Pending(ident, name, str(project or ""), _now(), str(run_id or ""))
        )
        _STATS["noted"] += 1
    return True


def _candidate(project: str, kind: str, run_id: str = ""):
    """Newest pending generation eligible for this verification, or None."""
    wanted = str(project or "")
    wanted_run = str(run_id or "")
    with _LOCK:
        for pending in reversed(_PENDING):
            if kind in pending.judged:
                continue
            # An empty project on either side means "unscoped" and is allowed
            # to match; two *different* named projects never match. Run
            # identity follows the same rule: it only ever narrows a match,
            # never widens one, so a caller with no run identity available
            # (the direct-call path) behaves exactly as before.
            if wanted and pending.project and wanted != pending.project:
                continue
            if wanted_run and pending.run_id and wanted_run != pending.run_id:
                continue
            return pending
    return None


def attribute(tool: str, ok: bool, project: str = "", record_fn=None, run_id: str = "") -> dict:
    """Attribute a verification result to the generation it most likely judges.

    `record_fn(interaction_id, signal)` performs the write; injected so this
    module has no import cycle with the server and is trivially testable.
    Returns a report describing what happened, including when nothing did.
    """
    name = str(tool or "").strip().lstrip("/")
    if name not in VERIFIERS:
        return {"attributed": False, "reason": "%s is not execution-grounded evidence" % name}
    _prune()
    pending = _candidate(project, name, run_id)
    if pending is None:
        with _LOCK:
            _STATS["unlinked"] += 1
        return {"attributed": False, "reason": "no recent generation to judge"}

    good_signal, bad_signal = VERIFIERS[name]
    signal = good_signal if ok else bad_signal
    with _LOCK:
        pending.judged.add(name)
        _STATS["attributed"] += 1
    report = {
        "attributed": True,
        "interaction_id": pending.interaction_id,
        "signal": signal,
        "verifier": name,
        "generator": pending.tool,
        "age_seconds": round(_now() - pending.created, 1),
    }
    if record_fn is not None:
        try:
            record_fn(pending.interaction_id, signal)
            report["recorded"] = True
        except Exception as exc:            # a failed write must not break the run
            report["recorded"] = False
            report["error"] = str(exc)
    return report


def pending_count() -> int:
    _prune()
    with _LOCK:
        return len(_PENDING)


def stats() -> dict:
    with _LOCK:
        return dict(_STATS)


def reset() -> None:
    """Clear all state. For tests and for a fresh session."""
    with _LOCK:
        _PENDING.clear()
        for key in _STATS:
            _STATS[key] = 0
